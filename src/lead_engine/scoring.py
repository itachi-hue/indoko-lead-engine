"""Achievement-only scoring, per group, as a percentile within the group (docs/DESIGN.md section 3).

Ported structure from the o1-leadgen prototype (saturating sub-scores, versioned snapshot hash,
percentile within segment). Removed: employer prestige, sponsorship history, wage proxies and any
immigration-related input. Inputs are public achievements only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from . import db
from .drafting.checks import numbers_in

SCORE_VERSION = "v3"

WEIGHTS = {
    "engineer": {"patents": 0.35, "talks": 0.20, "papers": 0.20, "projects": 0.10, "seniority": 0.15},
    "founder": {"funding": 0.45, "stage": 0.25, "press": 0.20, "recency": 0.10},
    "researcher": {"citations": 0.35, "h_index": 0.30, "recent_output": 0.20, "papers": 0.15},
    "creator": {"awards": 0.45, "press": 0.40, "breadth": 0.15},
}
STAGE = {"pre-seed": 0.3, "seed": 0.5, "series a": 0.75, "series b": 0.9, "series c": 1.0}
AWARD_RE = re.compile(r"\b(award|awards|prize|wins|won|winner|honou?r|fellowship|grant|selected|"
                      r"official selection|nominat\w+)\b", re.I)


def sat(x: float, k: float) -> float:
    """0..1 saturating curve; about 0.63 at x = k."""
    return 1 - math.exp(-x / k) if x and x > 0 else 0.0


def _signals(con, person_id: str) -> list:
    return con.execute("SELECT * FROM signals WHERE person_id=? ORDER BY observed_at", (person_id,)).fetchall()


def _latest(sig, kind: str, default=0.0) -> float:
    vals = [s["value"] for s in sig if s["signal_type"] == kind]
    try:
        return float(vals[-1]) if vals else default
    except (TypeError, ValueError):
        return default


def _count(sig, kind: str) -> int:
    return sum(1 for s in sig if s["signal_type"] == kind)


def subscores(person, sig) -> dict[str, float]:
    seg = person["segment"]
    if seg == "engineer":
        title = (person["title"] or "").lower()
        senior = 1.0 if re.search(r"distinguished|fellow|principal", title) else \
            0.7 if re.search(r"staff|director|head of", title) else 0.4
        return {"patents": sat(_count(sig, "patent"), 2), "talks": sat(_count(sig, "talk"), 1.5),
                "papers": sat(_count(sig, "paper"), 2), "projects": sat(_count(sig, "project"), 1.5),
                "seniority": senior}
    if seg == "founder":
        amounts = [max(numbers_in(s["value"] or "") or {0}) for s in sig if s["signal_type"] == "funding"]
        amount = max(amounts or [0])
        texts = " ".join((s["evidence_text"] or "").lower() for s in sig if s["signal_type"] == "funding")
        stage = max([v for k, v in STAGE.items() if k in texts] or [0.4])
        dates = sorted(s["evidence_date"] for s in sig if s["signal_type"] == "funding" and s["evidence_date"])
        recency = 1.0 if dates and dates[-1] >= _months_ago(6) else 0.5 if dates else 0.3
        return {"funding": sat(amount, 5_000_000), "stage": stage, "press": sat(_count(sig, "funding"), 2),
                "recency": recency}
    if seg == "researcher":
        return {"citations": sat(_latest(sig, "citations"), 1500), "h_index": sat(_latest(sig, "h_index"), 15),
                "recent_output": sat(_latest(sig, "recent_works_3y"), 8), "papers": sat(_latest(sig, "works_count"), 40)}
    if seg == "creator":
        press = [s for s in sig if s["signal_type"] == "press"]
        awards = sum(1 for s in press if AWARD_RE.search(s["evidence_text"] or ""))
        outlets = {s["value"] for s in press if s["value"]}
        return {"awards": sat(awards, 1.5), "press": sat(len(press), 2), "breadth": sat(len(outlets), 2)}
    return {}


def _months_ago(n: int) -> str:
    import datetime as dt

    return (dt.date.today() - dt.timedelta(days=30 * n)).isoformat()


def evidence_ok(con, person_id: str, minimum: int = 2) -> bool:
    """At least `minimum` citable evidence items, with a date."""
    return sum(1 for r in db.evidence_for(con, person_id) if r["evidence_date"]) >= minimum


def score_all(con, version: str = SCORE_VERSION) -> dict[str, int]:
    persons = con.execute("SELECT * FROM persons WHERE segment IN ('engineer','founder','researcher','creator')").fetchall()
    by_seg: dict[str, list] = {}
    for p in persons:
        sig = _signals(con, p["person_id"])
        sub = subscores(p, sig)
        weights = WEIGHTS[p["segment"]]
        raw = round(sum(weights[k] * sub.get(k, 0.0) for k in weights), 5)
        snap = hashlib.sha1(json.dumps([version, p["segment"], sorted((k, round(v, 4)) for k, v in sub.items())])
                            .encode()).hexdigest()[:16]
        by_seg.setdefault(p["segment"], []).append((p["person_id"], raw, sub, snap))
    for seg, recs in by_seg.items():
        vals = [r[1] for r in recs]
        n = len(vals)
        for pid, raw, sub, snap in recs:
            below = sum(1 for v in vals if v < raw)
            equal = sum(1 for v in vals if v == raw)
            pct = round(100 * (below + 0.5 * equal) / max(n, 1), 1)
            db.record_score(con, pid, version, seg, raw, pct, {k: round(v, 3) for k, v in sub.items()}, snap)
    con.commit()
    return {k: len(v) for k, v in by_seg.items()}
