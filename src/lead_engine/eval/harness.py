"""Evaluation harness (docs/DESIGN.md section 18).

Component checks: `sample` draws a random sample and writes a labelling sheet (CSV) to eval/data/;
a person fills in the label column; `score` computes the metric against its target.
System invariants: SQL checks over everything ever sent. Pilot: funnel per group + decision rules.
Labelling sheets contain personal data, so eval/data/ is git-ignored.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import random
from pathlib import Path

from .. import db, states, suppression
from ..drafting import checks
from ..emails.policy import violates_engineer_rule
from ..report import wilson
from . import labelled_drafts as ld

TARGETS = {
    "sourcing": "≥80% fit",
    "dedupe": "<2% duplicates and 0 wrong merges",
    "evidence": "≥95% correct",
    "scoring": "≥70% agreement",
    "emails": "wrong person ≤2%",
    "drafts": "≥70% approved unedited and average ≥4",
    "checks": "0 missed banned items and ≤10% false alarms",
}


def data_dir(ctx) -> Path:
    p = ctx.settings.root / "eval" / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(path: Path, header: list[str], rows: list[list]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def _ev(con, pid, n=3) -> str:
    return " | ".join(f"{r['evidence_text']} <{r['url']}>" for r in db.evidence_for(con, pid)[:n])


# ---------------- sampling ----------------
def sample(ctx, stage: str, n: int | None = None, seed: int | None = None) -> Path:
    con, rng = ctx.con, random.Random(seed)
    stamp = dt.date.today().isoformat()
    path = data_dir(ctx) / f"{stage}-{stamp}.csv"
    if stage == "sourcing":
        rows = []
        for seg in ("engineer", "founder", "researcher", "creator"):
            people = con.execute("SELECT * FROM persons WHERE segment=?", (seg,)).fetchall()
            for p in rng.sample(people, min(n or 25, len(people))):
                rows.append([p["person_id"], seg, p["display_name"], p["title"] or "", p["primary_employer"] or "",
                             p["country"] or "", _ev(con, p["person_id"]), ""])
        return _write(path, ["person_id", "group", "name", "title", "company", "location", "evidence",
                             "label_fit (yes/no)"], rows)
    if stage == "dedupe":
        people = con.execute("SELECT * FROM persons").fetchall()
        rows = []
        for p in rng.sample(people, min(n or 50, len(people))):
            ids = "; ".join(f"{r['id_type']}:{r['id_value']}" for r in con.execute(
                "SELECT id_type,id_value FROM person_identifiers WHERE person_id=?", (p["person_id"],)))
            rows.append([p["person_id"], p["display_name"], p["primary_employer"] or "", ids, "", ""])
        return _write(path, ["person_id", "name", "company", "identifiers", "label_duplicate_of (person_id or blank)",
                             "label_wrong_merge (yes/no)"], rows)
    if stage == "evidence":
        sigs = con.execute("SELECT s.*, p.display_name FROM signals s JOIN persons p ON p.person_id=s.person_id "
                           "WHERE s.evidence_text IS NOT NULL AND s.url IS NOT NULL").fetchall()
        rows = [[s["signal_id"], s["display_name"], s["evidence_text"], s["evidence_date"] or "", s["url"], ""]
                for s in rng.sample(sigs, min(n or 50, len(sigs)))]
        return _write(path, ["signal_id", "person", "fact", "date", "source", "label_correct (yes/no)"], rows)
    if stage == "scoring":
        rows = []
        scored = con.execute("SELECT p.person_id, p.display_name, p.segment, s.raw FROM persons p JOIN scores s "
                             "ON s.person_id=p.person_id").fetchall()
        by_seg: dict[str, list] = {}
        for r in scored:
            by_seg.setdefault(r["segment"], []).append(r)
        pairs = []
        for seg, ppl in by_seg.items():
            for _ in range(len(ppl)):
                if len(ppl) >= 2:
                    a, b = rng.sample(ppl, 2)
                    if a["raw"] != b["raw"]:
                        pairs.append((a, b))
        for a, b in rng.sample(pairs, min(n or 30, len(pairs))):
            rows.append([a["person_id"], b["person_id"], a["segment"], f"{a['display_name']}: {_ev(con, a['person_id'])}",
                         f"{b['display_name']}: {_ev(con, b['person_id'])}", ""])
        return _write(path, ["a_id", "b_id", "group", "A", "B", "label_stronger (A/B)"], rows)
    if stage == "emails":
        found = con.execute("SELECT * FROM persons WHERE email IS NOT NULL AND group_source='cold'").fetchall()
        rows = [[p["person_id"], p["display_name"], p["primary_employer"] or "", p["email"], p["email_source"] or "", ""]
                for p in rng.sample(found, min(n or 30, len(found)))]
        return _write(path, ["person_id", "name", "company", "email", "found_via", "label_right_person (yes/no)"], rows)
    if stage == "drafts":
        ds = con.execute("SELECT d.*, p.display_name FROM drafts d JOIN persons p ON p.person_id=d.person_id "
                         "WHERE d.channel='email' AND d.subject IS NOT NULL").fetchall()
        rows = [[d["draft_id"], d["segment"], d["display_name"], d["subject"], d["body"], "", "", "", ""]
                for d in rng.sample(ds, min(n or 50, len(ds)))]
        return _write(path, ["draft_id", "group", "name", "subject", "body", "label_relevance (1-5)",
                             "label_specificity (1-5)", "label_tone (1-5)", "label_decision (approve/edit)"], rows)
    raise ValueError(f"unknown stage: {stage}")


# ---------------- scoring a labelled sheet ----------------
def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _label(r: dict, prefix: str) -> str:
    for k, v in r.items():
        if k.startswith(prefix):
            return (v or "").strip().lower()
    return ""


def score(ctx, stage: str, path: Path) -> dict:
    rows = [r for r in _rows(path)]
    if stage == "sourcing":
        lab = [r for r in rows if _label(r, "label_fit") in ("yes", "no")]
        rate = sum(_label(r, "label_fit") == "yes" for r in lab) / len(lab) if lab else 0.0
        by = {}
        for seg in {r["group"] for r in lab}:
            g = [r for r in lab if r["group"] == seg]
            by[seg] = round(sum(_label(r, "label_fit") == "yes" for r in g) / len(g), 3)
        return _result(stage, len(lab), rate >= 0.80, {"fit_rate": round(rate, 3), "by_group": by})
    if stage == "dedupe":
        dup = sum(1 for r in rows if _label(r, "label_duplicate_of"))
        wrong = sum(1 for r in rows if _label(r, "label_wrong_merge") == "yes")
        rate = dup / len(rows) if rows else 0.0
        return _result(stage, len(rows), rate < 0.02 and wrong == 0,
                       {"duplicate_rate": round(rate, 3), "wrong_merges": wrong})
    if stage == "evidence":
        lab = [r for r in rows if _label(r, "label_correct") in ("yes", "no")]
        rate = sum(_label(r, "label_correct") == "yes" for r in lab) / len(lab) if lab else 0.0
        return _result(stage, len(lab), rate >= 0.95, {"correct_rate": round(rate, 3)})
    if stage == "scoring":
        lab = [r for r in rows if _label(r, "label_stronger") in ("a", "b")]
        agree = 0
        for r in lab:
            sa = ctx.con.execute("SELECT raw FROM scores WHERE person_id=?", (r["a_id"],)).fetchone()
            sb = ctx.con.execute("SELECT raw FROM scores WHERE person_id=?", (r["b_id"],)).fetchone()
            if sa and sb:
                model = "a" if sa["raw"] > sb["raw"] else "b"
                agree += model == _label(r, "label_stronger")
        rate = agree / len(lab) if lab else 0.0
        return _result(stage, len(lab), rate >= 0.70, {"agreement": round(rate, 3)})
    if stage == "emails":
        lab = [r for r in rows if _label(r, "label_right_person") in ("yes", "no")]
        wrong = sum(_label(r, "label_right_person") == "no" for r in lab) / len(lab) if lab else 0.0
        return _result(stage, len(lab), wrong <= 0.02 and bool(lab),
                       {"wrong_person_rate": round(wrong, 3), "find_rate_by_group": find_rates(ctx.con)})
    if stage == "drafts":
        lab = [r for r in rows if _label(r, "label_decision") in ("approve", "edit")]
        approved = sum(_label(r, "label_decision") == "approve" for r in lab) / len(lab) if lab else 0.0
        scores = []
        for r in lab:
            vals = [_label(r, p) for p in ("label_relevance", "label_specificity", "label_tone")]
            nums = [float(v) for v in vals if v.replace(".", "", 1).isdigit()]
            if nums:
                scores.append(sum(nums) / len(nums))
        avg = sum(scores) / len(scores) if scores else 0.0
        return _result(stage, len(lab), approved >= 0.70 and avg >= 4,
                       {"approved_unedited": round(approved, 3), "average_score": round(avg, 2)})
    raise ValueError(f"unknown stage: {stage}")


def _result(stage: str, n: int, passed: bool, metrics: dict) -> dict:
    return {"stage": stage, "labelled": n, "target": TARGETS[stage], "pass": bool(passed and n > 0), **metrics}


def find_rates(con) -> dict[str, dict]:
    out = {}
    for seg in ("engineer", "founder", "researcher", "creator"):
        tried = con.execute("SELECT COUNT(DISTINCT person_id) n FROM enrichment_ledger l WHERE person_id IN "
                            "(SELECT person_id FROM persons WHERE segment=?)", (seg,)).fetchone()["n"]
        found = con.execute("SELECT COUNT(*) n FROM persons WHERE segment=? AND email IS NOT NULL AND "
                            "group_source='cold'", (seg,)).fetchone()["n"]
        cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM enrichment_ledger WHERE person_id IN "
                           "(SELECT person_id FROM persons WHERE segment=?)", (seg,)).fetchone()["c"]
        out[seg] = {"tried": tried, "found": found, "find_rate": round(found / tried, 3) if tried else None,
                    "cost_per_found": round(cost / found, 3) if found else None}
    return out


# ---------------- the six checks on the labelled set ----------------
def checks_eval() -> dict:
    footer = 'Karishma, Indoko · address\nThis is a message about Indoko\'s services. Reply "unsubscribe"...'
    missed, alarms = [], []
    for item in ld.ALL:
        name = ld.NAMES[item["segment"]]
        cctx = checks.CheckContext(ld.EV[item["segment"]],
                                   [name, name.split()[0], "Karishma", "Indoko", ld.LINK, "30-minute 30 minutes"])
        res = checks.run_all(item["subject"], item["body"], item["fu1"], item["fu2"], cctx, footer)
        failed = {k for k, v in res.items() if not v["pass"]}
        if item["fails"]:
            missed += [f"{item['id']}:{c}" for c in item["fails"] if c not in failed]
        elif failed:
            alarms.append(item["id"])
    rate = len(alarms) / len(ld.GOOD)
    return {"stage": "checks", "labelled": len(ld.ALL), "target": TARGETS["checks"],
            "pass": not missed and rate <= 0.10, "missed": missed, "false_alarm_rate": round(rate, 3)}


# ---------------- system invariants ----------------
def invariants(ctx) -> dict:
    con = ctx.con
    violations: dict[str, list] = {}
    sends = con.execute("SELECT s.*, d.person_id, d.state, d.decided_at, d.segment FROM sends s "
                        "JOIN drafts d ON d.draft_id=s.draft_id WHERE s.status IN ('sent','exported','pending')").fetchall()
    for s in sends:
        p = db.get_person(con, s["person_id"])
        sent_to = s["email"] or (p["email"] if p else None)
        first_supp = con.execute(
            "SELECT MIN(added_at) t FROM suppression WHERE (match_type='email' AND value=?) OR "
            "(match_type='domain' AND value=?) OR (match_type='email_hash' AND value=?)",
            (sent_to or "", suppression.domain_of(sent_to) if sent_to else "",
             suppression.email_hash(sent_to) if sent_to else "")).fetchone()["t"]
        if first_supp and s["attempted_at"] and first_supp < s["attempted_at"]:
            violations.setdefault("sent_to_suppressed", []).append(s["send_id"])
        if p and sent_to and violates_engineer_rule(p, sent_to):
            violations.setdefault("engineer_corporate_domain", []).append(s["send_id"])
        if not s["decided_at"]:
            violations.setdefault("sent_without_approval", []).append(s["send_id"])
        ev = con.execute("SELECT MAX(finished_at) t FROM runs WHERE command='events' AND status='ok' "
                         "AND finished_at <= ?", (s["attempted_at"],)).fetchone()["t"]
        if s["attempted_at"] and (not ev or _hours(ev, s["attempted_at"]) > float(
                ctx.settings.get("sending.events_max_age_hours", 26))):
            violations.setdefault("sent_with_stale_events", []).append(s["send_id"])
    per_addr = con.execute("SELECT lower(email) e FROM sends WHERE email IS NOT NULL AND status IN "
                           "('sent','exported','pending') GROUP BY lower(email) HAVING COUNT(*) > 1").fetchall()
    if per_addr:
        violations["multiple_sends_per_address"] = [r["e"] for r in per_addr]
    dup = con.execute("SELECT draft_id FROM sends GROUP BY draft_id HAVING COUNT(*) > 1").fetchall()
    if dup:
        violations["multiple_sends_per_draft"] = [r["draft_id"] for r in dup]
    active = con.execute(f"SELECT person_id FROM drafts WHERE channel='email' AND state IN "
                         f"({','.join('?' * len(states.ACTIVE_STATES))}) GROUP BY person_id HAVING COUNT(*) > 1",
                         tuple(states.ACTIVE_STATES)).fetchall()
    if active:
        violations["multiple_active_drafts"] = [r["person_id"] for r in active]
    return {"checked_sends": len(sends), "pass": not violations, "violations": violations}


def _hours(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (dt.datetime.strptime(b, fmt) - dt.datetime.strptime(a, fmt)).total_seconds() / 3600


# ---------------- pilot review ----------------
def decide(positive: int, booked: int, bounces: int, sends: int) -> str:
    """Decision rules agreed before the pilot (docs/DESIGN.md section 18.4)."""
    if sends == 0:
        return "no data"
    delivered = max(sends - bounces, 1)
    pos, book, bounce = positive / delivered, booked / delivered, bounces / sends
    if bounce >= 0.03 or pos < 0.01:
        return "stop (or fix the email source first)" if bounce >= 0.03 else "stop"
    if pos >= 0.03 and book >= 0.01:
        return "scale up"
    return "fix targeting or the email, then retest"


def pilot(ctx) -> dict:
    con = ctx.con
    start = ctx.settings.get("sending.pilot_start", "") or "0000"
    out = {}
    for seg in ("engineer", "founder", "researcher", "creator", "warm"):
        people = [r["person_id"] for r in con.execute(
            "SELECT DISTINCT d.person_id FROM sends s JOIN drafts d ON d.draft_id=s.draft_id "
            "WHERE d.segment=? AND s.status IN ('sent','exported') AND COALESCE(s.sent_at,'') >= ?", (seg, start))]
        if not people:
            continue
        def count(t):
            return con.execute(f"SELECT COUNT(DISTINCT person_id) n FROM events WHERE type=? AND person_id IN "
                               f"({','.join('?' * len(people))})", (t, *people)).fetchone()["n"]
        sends, bounces, positive, booked = len(people), count("bounce"), count("positive_reply"), count("booked")
        clients = count("client")
        delivered = max(sends - bounces, 0)
        lo, hi = wilson(positive, delivered)
        out[seg] = {"sends": sends, "bounces": bounces, "positive_replies": positive, "booked": booked,
                    "clients": clients, "positive_rate": round(positive / delivered, 4) if delivered else 0.0,
                    "positive_range_95": [round(lo, 4), round(hi, 4)], "decision": decide(positive, booked, bounces, sends)}
    spend = con.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM runs WHERE started_at >= ?", (start,)).fetchone()["c"]
    clients = sum(v["clients"] for v in out.values())
    return {"groups": out, "spend_usd": round(spend, 2),
            "cost_per_client": round(spend / clients, 2) if clients else None,
            "note": "Small samples: treat differences inside the 95% ranges as noise. Clients close late; "
                    "re-run this review at week 8 or later."}


def save(ctx, result: dict) -> Path:
    path = data_dir(ctx) / "results.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": db.now(), **result}) + "\n")
    return path
