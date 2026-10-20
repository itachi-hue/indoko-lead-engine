import csv
import datetime as dt

import pytest

from lead_engine import db, suppression
from lead_engine.eval import harness
from lead_engine.instantly import sender
from lead_engine.runtime import Run

from conftest import make_person


def _fill(path, label_col, values):
    rows = list(csv.DictReader(path.open()))
    for r, v in zip(rows, values):
        r[label_col] = v
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _people(con, seg, n):
    out = []
    for i in range(n):
        pid = make_person(con, name=f"{seg.title()} P{i}", segment=seg, email=f"{seg}{i}@x.ai")
        db.add_signal(con, pid, "paper", i, f"u{seg}{i}", evidence_text=f"Paper {i}", evidence_date="2026-01-01",
                      url=f"https://x/{seg}{i}")
        out.append(pid)
    con.commit()
    return out


def test_sourcing_sample_and_score(ctx):
    _people(ctx.con, "founder", 10)
    path = harness.sample(ctx, "sourcing", n=10, seed=1)
    assert path.parent.name == "data" and path.read_text().startswith("person_id,group,name")
    _fill(path, "label_fit (yes/no)", ["yes"] * 9 + ["no"])
    res = harness.score(ctx, "sourcing", path)
    assert res["fit_rate"] == 0.9 and res["pass"] and res["by_group"] == {"founder": 0.9}


def test_evidence_score_fails_below_target(ctx):
    _people(ctx.con, "researcher", 20)
    path = harness.sample(ctx, "evidence", n=20, seed=2)
    _fill(path, "label_correct (yes/no)", ["yes"] * 18 + ["no"] * 2)
    res = harness.score(ctx, "evidence", path)
    assert res["correct_rate"] == 0.9 and not res["pass"]


def test_scoring_agreement(ctx):
    from lead_engine import scoring

    a = make_person(ctx.con, name="Strong S", segment="engineer", title="Principal Engineer")
    b = make_person(ctx.con, name="Weak W", segment="engineer", title="Engineer", email="w@x.ai")
    for i in range(3):
        db.add_signal(ctx.con, a, "patent", 1, f"p{i}", evidence_text=f"Patent {i}", evidence_date="2025-01-01", url=f"p{i}")
    scoring.score_all(ctx.con)
    path = harness.sample(ctx, "scoring", n=5, seed=3)
    rows = list(csv.DictReader(path.open()))
    truth = ["A" if r["a_id"] == a else "B" for r in rows]
    _fill(path, "label_stronger (A/B)", truth)
    assert harness.score(ctx, "scoring", path)["agreement"] == 1.0


def test_drafts_score(ctx, tmp_path):
    path = tmp_path / "drafts.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["draft_id", "label_relevance (1-5)", "label_specificity (1-5)", "label_tone (1-5)",
                    "label_decision (approve/edit)"])
        for i in range(10):
            w.writerow([f"d{i}", 5, 4, 4, "approve" if i < 7 else "edit"])
    res = harness.score(ctx, "drafts", path)
    assert res["approved_unedited"] == 0.7 and res["average_score"] == 4.33 and res["pass"]


def test_checks_eval_on_labelled_set():
    res = harness.checks_eval()
    assert res["pass"] and res["missed"] == [] and res["labelled"] == 40


def _send(con, pid, when, decided=True, email=None):
    did = db.new_id("d")
    con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,state,decided_at,created_at,updated_at) "
                "VALUES(?,?,?,?,'sent',?,?,?)", (did, pid, "b", "founder", when if decided else None, when, when))
    con.execute("INSERT INTO sends(send_id,draft_id,campaign_id,email,method,status,attempted_at,sent_at) "
                "VALUES(?,?,?,?,'api','sent',?,?)", (db.new_id("s"), did, "c", email, when, when))
    con.commit()


def test_invariants_clean_and_violations(ctx):
    con = ctx.con
    con.execute("INSERT INTO runs(run_id,command,started_at,finished_at,status) VALUES "
                "('e1','events','2026-11-24T07:00:00Z','2026-11-24T07:00:00Z','ok')")
    p = make_person(con, segment="founder", email="ok@x.ai")
    _send(con, p, "2026-11-24T08:00:00Z", email="ok@x.ai")
    assert harness.invariants(ctx)["pass"]
    # sent to an address that was already suppressed, without approval, with stale events
    q = make_person(con, name="Q Q", segment="founder", email="q@x.ai")
    suppression.add(con, "email", "q@x.ai", "unsub", "manual")
    con.execute("UPDATE suppression SET added_at='2026-11-01T00:00:00Z'")
    _send(con, q, "2026-11-28T08:00:00Z", decided=False, email="q@x.ai")
    eng = make_person(con, name="E E", segment="engineer", email="e@google.com")
    _send(con, eng, "2026-11-24T09:00:00Z", email="e@google.com")
    v = harness.invariants(ctx)["violations"]
    assert set(v) == {"sent_to_suppressed", "sent_without_approval", "sent_with_stale_events",
                      "engineer_corporate_domain"}


def test_send_blocked_by_invariant_violation(ctx):
    con = ctx.con
    eng = make_person(con, name="E E", segment="engineer", email="e@google.com")
    _send(con, eng, db.now(), email="e@google.com")
    with Run(con, "events"):
        pass
    with pytest.raises(sender.SendBlocked, match="invariant"):
        sender.preflight(ctx)


@pytest.mark.parametrize("pos,booked,bounces,sends,expected", [
    (4, 2, 1, 100, "scale up"),
    (2, 0, 1, 100, "fix targeting or the email, then retest"),
    (0, 0, 1, 100, "stop"),
    (5, 2, 4, 100, "stop (or fix the email source first)"),
    (0, 0, 0, 0, "no data"),
])
def test_decision_rules(pos, booked, bounces, sends, expected):
    assert harness.decide(pos, booked, bounces, sends) == expected


def test_pilot_review(ctx):
    con = ctx.con
    ctx.settings.data["sending"]["pilot_start"] = "2026-11-01"
    ids = [make_person(con, name=f"F {i}", segment="founder", email=f"f{i}@x.ai") for i in range(100)]
    for pid in ids:
        _send(con, pid, "2026-11-24T08:00:00Z")
    for pid in ids[:4]:
        db.add_event(con, pid, "positive_reply", "instantly")
    for pid in ids[:2]:
        db.add_event(con, pid, "booked", "crm")
    db.add_event(con, ids[0], "client", "crm")
    con.commit()
    out = harness.pilot(ctx)["groups"]["founder"]
    assert out["decision"] == "scale up" and out["clients"] == 1
    assert out["positive_range_95"][0] < 0.04 < out["positive_range_95"][1]
