"""Regression tests for the bugs found in the independent code review."""
import json

import pytest

from lead_engine import db, states, suppression
from lead_engine.cli import main
from lead_engine.drafting import drafter
from lead_engine.eval import harness
from lead_engine.http import HTTPError
from lead_engine.instantly import events, sender
from lead_engine.runtime import Run
from lead_engine.sheets import crm, review
from lead_engine.sources import warm

from conftest import make_person
from fakes import good_output
from test_flow import TODAY, approved, drafted, events_ok, run_cmd, sent_one, world  # noqa: F401


def _needs_manual(world):
    founder = make_person(world.con, name="Ada Lovelace", segment="founder", email="ada@northwind.ai")
    bad = good_output(body="You would qualify for the O-1. https://cal.com/indoko/founder")
    world.gemini.outputs = [bad, bad]
    run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=5)
    run_cmd(world, "review", review.push)
    return world.con.execute("SELECT draft_id FROM drafts").fetchone()["draft_id"], founder


# 1 + 8: a needs_manual draft can never go out blank or with blank follow-ups
def test_approving_needs_manual_without_text_is_refused(world):
    did, _ = _needs_manual(world)
    world.sheets.set_cell("Review", did, "Decision", "Approve")
    counts, _ = run_cmd(world, "review", review.pull)
    assert counts["failed_recheck"] == 1
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.PUSHED


def test_needs_manual_requires_all_four_fields(world):
    did, _ = _needs_manual(world)
    world.sheets.set_cell("Review", did, "Decision", "Edit")
    world.sheets.set_cell("Review", did, "Edited subject", "Northwind")
    world.sheets.set_cell("Review", did, "Edited body", "Hi Ada, a 30-minute consultation? https://cal.com/indoko/founder")
    run_cmd(world, "review", review.pull)
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.PUSHED
    world.sheets.set_cell("Review", did, "Edited follow-up 1", "Hi Ada, a short follow-up.")
    world.sheets.set_cell("Review", did, "Edited follow-up 2", "Hi Ada, last note from me.")
    run_cmd(world, "review", review.pull)
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.EDITED


def test_sender_refuses_empty_text(world):
    (a,) = approved(world)
    world.con.execute("UPDATE drafts SET fu2='', edited_fu2=NULL WHERE draft_id=?", (a,))
    world.con.commit()
    events_ok(world)
    run_cmd(world, "send", sender.send, today=TODAY)
    assert world.instantly.added == []
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.STALE


# 2: suppressed while a send was pending -> never retried
def test_pending_send_rechecks_suppression(world):
    approved(world)
    events_ok(world)
    world.instantly.fail_add_with = 503
    run_cmd(world, "send", sender.send, today=TODAY)
    assert main(["suppress", "ada0@northwind.ai"], ctx=world) == 0
    world.instantly.fail_add_with = None
    run_cmd(world, "send", sender.send, today=TODAY)
    assert world.instantly.added == []
    s = world.con.execute("SELECT status, error FROM sends").fetchone()
    assert s["status"] == "failed" and s["error"] == "suppressed"


# 3: one address, two person records -> emailed once at most
def test_same_address_two_people_sent_once(world):
    con = world.con
    a = make_person(con, name="Bob One", segment="warm", email="bob@acme.io", group_source="warm")
    b = make_person(con, name="Bob Two", segment="warm", email=None, group_source="warm")
    db.touch_person(con, b, email="bob@acme.io", email_status="on_file")       # conflict-created record
    con.commit()
    assert [p["person_id"] for p in drafter.eligible_people(con, "warm", 10)] == [a]
    for pid in (a, b):
        con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,subject,body,fu1,fu2,state,decided_at,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'approved',?,?,?)",
                    (db.new_id("d"), pid, "b", "warm", "S", "B", "F1", "F2", db.now(), db.now(), db.now()))
    con.commit()
    events_ok(world)
    run_cmd(world, "send", sender.send, today=TODAY)
    assert len(world.instantly.added) == 1
    assert harness.invariants(world)["pass"]


def test_invariant_flags_two_sends_to_one_address(world):
    con = world.con
    for name in ("A A", "B B"):
        pid = make_person(con, name=name, segment="founder", email=None)
        did = db.new_id("d")
        con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,state,decided_at,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'sent', ?,?,?)", (did, pid, "b", "founder", db.now(), db.now(), db.now()))
        con.execute("INSERT INTO sends(send_id,draft_id,campaign_id,email,method,status,attempted_at) "
                    "VALUES(?,?,?,?,'api','sent',?)", (db.new_id("s"), did, "c", "same@x.ai", db.now()))
    con.commit()
    assert "multiple_sends_per_address" in harness.invariants(world)["violations"]


# 4: HTML-only unsubscribe replies
def test_html_only_unsubscribe(world):
    sent_one(world)
    world.instantly.received = [{"id": "m1", "from_address_email": "ada0@northwind.ai",
                                 "timestamp_created": "2026-11-25T00:00:00Z",
                                 "body": {"html": "<div>Please unsubscribe me</div><blockquote>…</blockquote>"}}]
    run_cmd(world, "events", events.poll_api)
    assert suppression.is_suppressed(world.con, "ada0@northwind.ai")


# 5: flags accumulate and clear only when the team tags a reason
def test_flags_merge_and_clear(world):
    sent_one(world)
    pid = world.con.execute("SELECT person_id FROM drafts").fetchone()["person_id"]
    world.instantly.received = [{"id": "m1", "from_address_email": "ada0@northwind.ai",
                                 "timestamp_created": "2026-11-25T00:00:00Z", "body": {"text": "Not interested"}}]
    main(["events"], ctx=world)
    world.instantly.received = []
    main(["events"], ctx=world)                                     # a run with no new flags
    assert pid in json.loads(db.get_cursor(world.con, "control", "crm_flags"))
    main(["crm", "push"], ctx=world)
    world.sheets.set_cell("CRM", pid, "Reply reason", "not relevant")
    main(["crm", "pull"], ctx=world)
    assert pid not in json.loads(db.get_cursor(world.con, "control", "crm_flags"))


# 6: a failing run keeps nothing half-done
def test_failed_events_run_rolls_back(world):
    lead = sent_one(world)
    lead["status"] = -2

    def boom(since=None):
        raise RuntimeError("API down")
    world.instantly.list_received_emails = boom
    with pytest.raises(RuntimeError):
        run_cmd(world, "events", events.poll_api)
    assert not suppression.is_suppressed(world.con, "ada0@northwind.ai")   # rolled back, so retried next run
    world.instantly.list_received_emails = lambda since=None: []
    run_cmd(world, "events", events.poll_api)
    assert suppression.is_suppressed(world.con, "ada0@northwind.ai")
    assert world.instantly.blocked == ["ada0@northwind.ai"]


# 7: forget can't be undone by the Suppression tab
def test_forget_survives_suppression_tab(world):
    sent_one(world)
    world.sheets.tabs["Suppression"].append(["ada0@northwind.ai", "asked", "K", "2026-11-01"])
    main(["forget", "ada0@northwind.ai"], ctx=world)
    assert world.sheets.tabs["Suppression"][1][0] == ""
    world.sheets.tabs["Suppression"].append(["ada0@northwind.ai", "again", "K", "2026-11-02"])
    crm.pull_suppression(world)
    plain = world.con.execute("SELECT COUNT(*) n FROM suppression WHERE match_type='email'").fetchone()["n"]
    assert plain == 0 and suppression.is_suppressed(world.con, "ada0@northwind.ai")


# 9: a non-retryable error keeps approvals and stops the run
def test_non_retryable_error_keeps_approvals(world):
    approved(world, 3)
    events_ok(world)
    world.instantly.fail_add_with = 402
    with pytest.raises(sender.SendBlocked, match="HTTP 402"):
        run_cmd(world, "send", sender.send, today=TODAY)
    assert {r["state"] for r in world.con.execute("SELECT state FROM drafts")} == {states.APPROVED}
    assert world.con.execute("SELECT COUNT(*) n FROM sends").fetchone()["n"] == 0


# 11: CRM dates with a time
def test_crm_date_with_time(world):
    sent_one(world)
    pid = world.con.execute("SELECT person_id FROM drafts").fetchone()["person_id"]
    run_cmd(world, "crm", crm.push)
    world.sheets.set_cell("CRM", pid, "Booked", "2026-11-25 14:00")
    run_cmd(world, "crm", crm.pull)
    ev = world.con.execute("SELECT occurred_at FROM events WHERE type='booked'").fetchone()
    assert ev["occurred_at"] == "2026-11-25T00:00:00Z"


# design gaps
def test_domain_suppression_covers_subdomains(con):
    suppression.add(con, "domain", "acme.com", "x", "manual")
    assert suppression.is_suppressed(con, "a@eu.acme.com") and not suppression.is_suppressed(con, "a@notacme.com")


def test_suppression_tab_stops_drafts_in_review(world):
    (a,) = drafted(world)
    run_cmd(world, "review", review.push)
    world.sheets.tabs["Suppression"].append(["northwind.ai", "asked", "K", ""])
    crm.pull_suppression(world)
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.STALE


def test_review_pull_dry_run_changes_nothing(world):
    (a,) = drafted(world)
    run_cmd(world, "review", review.push)
    world.sheets.set_cell("Review", a, "Decision", "Approve")
    world.dry_run = True
    run_cmd(world, "review", review.pull)
    world.dry_run = False
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.PUSHED


def test_csv_pending_recovered_after_crash(world):
    world.settings.data["instantly"]["mode"] = "csv"
    (a,) = approved(world)
    events_ok(world)
    d = world.con.execute("SELECT * FROM drafts").fetchone()
    sender._queue(world.con, d, db.get_person(world.con, d["person_id"]), "camp-fnd", "csv")   # crash after step 1
    summary, _ = run_cmd(world, "send", sender.send, today=TODAY)
    assert len(summary["csv_files"]) == 1
    assert world.con.execute("SELECT status FROM sends").fetchone()["status"] == "exported"


def test_warm_conflict_record_not_drafted(con):
    a = warm.import_rows(con, [{"email": "bob@acme.io", "name": "Bob One"}], "l1", assume_us=True)
    pid_b = db.create_person(con, "Bob Li", segment="warm")
    db.add_identifier(con, pid_b, "linkedin", "linkedin.com/in/bob")
    con.commit()
    warm.import_rows(con, [{"email": "bob@acme.io", "name": "Bob", "linkedin": "linkedin.com/in/bob"}], "l2",
                     assume_us=True)
    assert a.imported == 1
    assert len(drafter.eligible_people(con, "warm", 10)) == 1
