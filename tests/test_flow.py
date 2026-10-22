"""End-to-end flow with fakes: draft -> review -> send -> events -> CRM -> forget."""
import datetime as dt
import json

import pytest

from lead_engine import db, states, suppression
from lead_engine.cli import main
from lead_engine.drafting import drafter
from lead_engine.drafting.gemini import GeminiError
from lead_engine.instantly import events, sender
from lead_engine.runtime import Run
from lead_engine.sheets import crm, layout, review

from conftest import make_person
from fakes import FakeGemini, FakeInstantly, FakeSheets, good_output

TODAY = dt.date(2026, 11, 23)


@pytest.fixture
def world(ctx):
    ctx.sheets = FakeSheets()
    ctx.gemini = FakeGemini()
    ctx.instantly = FakeInstantly()
    ctx.sheets.ensure_tabs(list(layout.tabs(ctx.settings)))
    for tab, cols in layout.tabs(ctx.settings).items():
        ctx.sheets.tabs[tab] = [list(cols)]
    ctx.settings.data["instantly"]["mode"] = "api"
    return ctx


def founder(con, name="Ada Lovelace", email="ada@northwind.ai"):
    pid = make_person(con, name=name, segment="founder", email=email, employer="Northwind",
                      employer_domain="northwind.ai")
    db.add_signal(con, pid, "funding", "4000000", "exa:news1",
                  evidence_text="Northwind raised a seed round in September 2026", evidence_date="2026-09-10",
                  url="https://news.example/northwind")
    con.commit()
    return pid


def run_cmd(ctx, name, fn, **kw):
    with Run(ctx.con, name) as r:
        out = fn(ctx, r, **kw) if kw else fn(ctx, r)
    return out, r


def events_ok(ctx):
    with Run(ctx.con, "events"):
        pass


# ---------------- drafting ----------------
def test_draft_passes_first_time(world):
    pid = founder(world.con)
    sid = db.evidence_for(world.con, pid)[0]["signal_id"]
    world.gemini.outputs = [good_output([sid])]
    _, r = run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=10)
    d = world.con.execute("SELECT * FROM drafts").fetchone()
    assert r.ok == 1 and d["state"] == states.DRAFTED and json.loads(d["evidence_ids"]) == [sid]
    assert "Northwind raised a seed round" in world.gemini.prompts[0]
    assert "ada@" not in world.gemini.prompts[0]           # emails are never sent to Gemini


def test_draft_regenerates_once_then_needs_manual(world):
    founder(world.con)
    bad = good_output(body="You would qualify for the O-1. Book now? Call? https://cal.com/indoko/founder")
    world.gemini.outputs = [bad, bad]
    _, r = run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=10)
    d = world.con.execute("SELECT * FROM drafts").fetchone()
    assert d["state"] == states.NEEDS_MANUAL and d["body"] is None and r.skipped == 1
    assert "previous draft failed" in world.gemini.prompts[1].lower()


def test_draft_gemini_error_counts_as_failure(world):
    founder(world.con)
    world.gemini.outputs = [GeminiError("blocked"), GeminiError("blocked")]
    run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=10)
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.NEEDS_MANUAL


def test_eligibility_filters(world):
    con = world.con
    a = founder(con)
    founder(con, name="Non US", email="n@x.ai")
    con.execute("UPDATE persons SET us_located=0 WHERE email='n@x.ai'")
    founder(con, name="Sup", email="s@x.ai")
    suppression.add(con, "email", "s@x.ai", "unsub", "manual")
    contacted = founder(con, name="Old", email="o@x.ai")
    db.add_event(con, contacted, "sent", "instantly")
    con.commit()
    people = drafter.eligible_people(con, "founder", 10)
    assert [p["person_id"] for p in people] == [a]
    world.gemini.outputs = [good_output()]
    run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=10)
    assert drafter.eligible_people(con, "founder", 10) == []   # one active draft per person


def test_drafting_refuses_without_footer(world):
    founder(world.con)
    world.settings.data["sender"]["postal_address"] = ""
    with pytest.raises(RuntimeError):
        run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=10)


# ---------------- review ----------------
def drafted(world, n=1):
    ids = []
    for i in range(n):
        founder(world.con, name=f"Ada {i}", email=f"ada{i}@northwind.ai")
    world.gemini.outputs = [good_output() for _ in range(n)]
    run_cmd(world, "draft", drafter.draft_batch, segment="founder", limit=n)
    for r in world.con.execute("SELECT draft_id FROM drafts ORDER BY created_at"):
        ids.append(r["draft_id"])
    return ids


def test_review_push_pull_approve_reject_edit(world):
    a, b, c = drafted(world, 3)
    run_cmd(world, "review", review.push)
    tab = "Review"
    assert len(world.sheets.tabs[tab]) == 4
    world.sheets.set_cell(tab, a, "Decision", "Approve")
    world.sheets.set_cell(tab, b, "Decision", "Reject")
    world.sheets.set_cell(tab, c, "Decision", "Edit")
    world.sheets.set_cell(tab, c, "Edited subject", "Northwind and the US")
    counts, _ = run_cmd(world, "review", review.pull)
    st = {r["draft_id"]: r["state"] for r in world.con.execute("SELECT draft_id, state FROM drafts")}
    assert st == {a: states.APPROVED, b: states.REJECTED, c: states.EDITED}
    assert counts["approved"] == counts["rejected"] == counts["edited"] == 1
    assert world.sheets.row_by_id(tab, a)[layout.REVIEW_COLUMNS.index("State")] == states.APPROVED
    d = world.con.execute("SELECT * FROM drafts WHERE draft_id=?", (c,)).fetchone()
    assert states.final_text(d)["subject"] == "Northwind and the US"


def test_review_edit_failing_checks_stays_pushed(world):
    (a,) = drafted(world)
    run_cmd(world, "review", review.push)
    world.sheets.set_cell("Review", a, "Decision", "Edit")
    world.sheets.set_cell("Review", a, "Edited body", "You definitely qualify for the EB-1A!")
    counts, _ = run_cmd(world, "review", review.pull)
    assert counts["failed_recheck"] == 1
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.PUSHED
    notes = world.sheets.row_by_id("Review", a)[layout.REVIEW_COLUMNS.index("Notes")]
    assert "no_eligibility" in notes


def test_review_does_not_apply_against_changed_state(world):
    (a,) = drafted(world)
    run_cmd(world, "review", review.push)
    world.sheets.set_cell("Review", a, "Decision", "Approve")
    world.con.execute("UPDATE drafts SET state=? WHERE draft_id=?", (states.STALE, a))  # suppressed meanwhile
    world.con.commit()
    run_cmd(world, "review", review.pull)
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.STALE


def test_review_repushes_deleted_rows_and_ignores_late_edits(world):
    (a,) = drafted(world)
    run_cmd(world, "review", review.push)
    world.sheets.tabs["Review"] = world.sheets.tabs["Review"][:1]       # someone deleted the row
    n, _ = run_cmd(world, "review", review.push)
    assert n == 1 and world.sheets.row_by_id("Review", a)
    world.con.execute("UPDATE drafts SET state=? WHERE draft_id=?", (states.QUEUED, a))
    world.con.commit()
    world.sheets.set_cell("Review", a, "Edited body", "late change")
    counts, _ = run_cmd(world, "review", review.pull)
    assert counts["ignored_locked"] == 1
    assert world.con.execute("SELECT edited_body FROM drafts").fetchone()["edited_body"] is None
    assert review.protect_locked_rows(world) == 1 and review.protect_locked_rows(world) == 0


# ---------------- sending ----------------
def approved(world, n=1):
    ids = drafted(world, n)
    world.con.execute(f"UPDATE drafts SET state='approved', decided_at=? WHERE draft_id IN "
                      f"({','.join('?' * len(ids))})", (db.now(), *ids))
    world.con.commit()
    return ids


def test_preflight_blocks(world):
    approved(world)
    with pytest.raises(sender.SendBlocked, match="never succeeded"):
        sender.preflight(world, TODAY)
    events_ok(world)
    db.set_cursor(world.con, "control", "sending_paused", "bounces")
    with pytest.raises(sender.SendBlocked, match="paused"):
        sender.preflight(world, TODAY)
    world.con.execute("DELETE FROM crawl_cursors WHERE source='control'")
    world.settings.data["sending"]["inboxes"] = 0
    with pytest.raises(sender.SendBlocked, match="inboxes"):
        sender.preflight(world, TODAY)


def test_preflight_blocks_stale_events(world):
    world.con.execute("INSERT INTO runs(run_id,command,started_at,finished_at,status) VALUES "
                      "('r1','events','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','ok')")
    world.con.commit()
    with pytest.raises(sender.SendBlocked, match="limit 26h"):
        sender.preflight(world, TODAY)


def test_failed_events_run_does_not_count(world):
    world.settings.data["instantly"]["mode"] = "csv"
    assert main(["events"], ctx=world) == 1
    with pytest.raises(sender.SendBlocked, match="never succeeded"):
        sender.preflight(world, TODAY)


def test_send_api_happy_path(world):
    (a,) = approved(world)
    events_ok(world)
    summary, r = run_cmd(world, "send", sender.send, today=TODAY)
    assert summary["queued"] == 1 and r.ok == 1
    s = world.con.execute("SELECT * FROM sends").fetchone()
    assert s["status"] == "sent" and s["instantly_lead_id"] == "lead1"
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.SENT
    cv = world.instantly.added[0]["custom_variables"]
    assert cv["draft_id"] == a and "unsubscribe" in cv["body_html"] and "<br>" in cv["body_html"]
    assert "1 Market St" in cv["fu1_html"]


def test_send_is_never_duplicated(world):
    approved(world)
    events_ok(world)
    run_cmd(world, "send", sender.send, today=TODAY)
    run_cmd(world, "send", sender.send, today=TODAY)
    assert len(world.instantly.added) == 1


def test_send_duplicate_in_workspace(world):
    approved(world)
    events_ok(world)
    world.instantly.skip_emails = {"ada0@northwind.ai"}
    run_cmd(world, "send", sender.send, today=TODAY)
    s = world.con.execute("SELECT * FROM sends").fetchone()
    assert s["status"] == "failed" and s["error"] == "duplicate"
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.STALE


def test_send_rechecks_suppression_and_engineer_rule(world):
    approved(world, 2)
    events_ok(world)
    suppression.add(world.con, "email", "ada0@northwind.ai", "unsub", "manual")
    world.con.execute("UPDATE persons SET segment='engineer', email='ada1@google.com' WHERE email='ada1@northwind.ai'")
    world.con.execute("UPDATE drafts SET segment='engineer' WHERE person_id IN "
                      "(SELECT person_id FROM persons WHERE email='ada1@google.com')")
    world.con.commit()
    _, r = run_cmd(world, "send", sender.send, today=TODAY)
    assert world.instantly.added == [] and r.skipped == 2
    assert {x["state"] for x in world.con.execute("SELECT state FROM drafts")} == {states.STALE}


def test_send_respects_daily_cap(world):
    world.settings.data["sending"]["inboxes"] = 1        # week 1: 15 a day
    approved(world, 17)
    events_ok(world)
    summary, _ = run_cmd(world, "send", sender.send, today=TODAY)
    assert summary["queued"] == 15 and len(world.instantly.added) == 15
    assert sender.daily_cap(world.settings, TODAY + dt.timedelta(days=14)) == 35


def test_send_csv_mode(world):
    world.settings.data["instantly"]["mode"] = "csv"
    approved(world, 2)
    events_ok(world)
    summary, _ = run_cmd(world, "send", sender.send, today=TODAY)
    assert len(summary["csv_files"]) == 1
    text = open(summary["csv_files"][0]).read()
    assert text.startswith("email,first_name,subject,body_html") and "ada1@northwind.ai" in text
    assert {r["status"] for r in world.con.execute("SELECT status FROM sends")} == {"exported"}


def test_recover_pending_after_crash(world):
    (a,) = approved(world)
    events_ok(world)
    world.instantly.fail_add_with = 503          # retryable: left pending
    run_cmd(world, "send", sender.send, today=TODAY)
    assert world.con.execute("SELECT status FROM sends").fetchone()["status"] == "pending"
    world.instantly.fail_add_with = None
    run_cmd(world, "send", sender.send, today=TODAY)
    assert world.con.execute("SELECT status FROM sends").fetchone()["status"] == "sent"
    assert len(world.instantly.added) == 1


def test_recover_pending_finds_existing_lead(world):
    (a,) = approved(world)
    events_ok(world)
    with Run(world.con, "send") as r:
        d = world.con.execute("SELECT * FROM drafts").fetchone()
        p = db.get_person(world.con, d["person_id"])
        sender._queue(world.con, d, p, "camp-fnd", "api")       # crash right after step 1 ...
    world.instantly.add_lead("camp-fnd", p["email"], "Ada", {})  # ... but Instantly did get the lead
    run_cmd(world, "send", sender.send, today=TODAY)
    assert len(world.instantly.added) == 1
    assert world.con.execute("SELECT status FROM sends").fetchone()["status"] == "sent"


# ---------------- events ----------------
def sent_one(world):
    approved(world)
    events_ok(world)
    run_cmd(world, "send", sender.send, today=TODAY)
    return world.instantly.added[0]


def test_events_mapping_and_idempotency(world):
    lead = sent_one(world)
    lead.update({"timestamp_last_contact": "2026-11-24T10:00:00Z", "email_reply_count": 1,
                 "timestamp_last_reply": "2026-11-25T10:00:00Z", "lt_interest_status": 1})
    run_cmd(world, "events", events.poll_api)
    run_cmd(world, "events", events.poll_api)
    types = sorted(r["type"] for r in world.con.execute("SELECT type FROM events"))
    assert types == ["positive_reply", "reply", "sent"]


def test_events_unsubscribe_suppresses_blocks_and_closes(world):
    lead = sent_one(world)
    lead["status"] = -2
    run_cmd(world, "events", events.poll_api)
    assert suppression.is_suppressed(world.con, "ada0@northwind.ai")
    assert world.instantly.blocked == ["ada0@northwind.ai"]
    assert world.con.execute("SELECT state FROM drafts").fetchone()["state"] == states.CLOSED
    with Run(world.con, "x") as r:
        assert sender.remove_finished(world, r) == 1
    assert world.instantly.deleted == [lead["id"]]


def test_unsubscribe_reply_and_quoted_footer(world):
    sent_one(world)
    world.instantly.received = [
        {"id": "m1", "from_address_email": "ada0@northwind.ai", "timestamp_created": "2026-11-25T00:00:00Z",
         "body": {"text": "Sounds good, let's talk.\n\nOn Mon, Karishma wrote:\n> Reply \"unsubscribe\" and we won't"}},
    ]
    run_cmd(world, "events", events.poll_api)
    assert not suppression.is_suppressed(world.con, "ada0@northwind.ai")
    world.instantly.received.append({"id": "m2", "from_address_email": "ada0@northwind.ai",
                                     "timestamp_created": "2026-11-26T00:00:00Z",
                                     "body": {"text": "Please remove me from your list"}})
    run_cmd(world, "events", events.poll_api)
    assert suppression.is_suppressed(world.con, "ada0@northwind.ai")


def test_classify_reply():
    assert events.classify_reply("STOP") == "unsubscribe"
    assert events.classify_reply("Not interested, thanks") == "ambiguous"
    assert events.classify_reply("Yes, Tuesday works") == "reply"


def test_events_csv_import(world):
    world.settings.data["instantly"]["mode"] = "csv"
    approved(world, 2)
    events_ok(world)
    run_cmd(world, "send", sender.send, today=TODAY)
    csv_text = ("Email,Lead Status,Interest Status,Reply Count,Last Contacted\n"
                "ada0@northwind.ai,bounced,,0,2026-11-24\n"
                "ada1@northwind.ai,active,interested,1,2026-11-24\n")
    res, _ = run_cmd(world, "events", events.import_csv, text=csv_text)
    assert res["suppressed"] == 1 and suppression.is_suppressed(world.con, "ada0@northwind.ai")
    export = world.settings.data_dir / "exports" / "suppression-export.csv"
    assert "ada0@northwind.ai" in export.read_text()
    types = sorted(r["type"] for r in world.con.execute("SELECT type FROM events"))
    assert types == ["bounce", "positive_reply", "reply", "sent", "sent"]


def _fake_sends(con, pid, n):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(n):
        did = db.new_id("d")
        con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)", (did, pid, "b", "founder", "sent", now, now))
        con.execute("INSERT INTO sends(send_id,draft_id,campaign_id,method,status,sent_at) VALUES(?,?,?,?,?,?)",
                    (db.new_id("s"), did, "camp-fnd", "api", "sent", now))
    con.commit()


def test_bounce_guard_needs_minimum_sends(world):
    con = world.con
    pid = founder(con, name="B", email="b@x.ai")
    _fake_sends(con, pid, 40)
    for _ in range(10):
        db.add_event(con, pid, "bounce", "instantly")
    con.commit()
    with Run(con, "events") as r:
        assert events.bounce_guard(world, r) is False         # 25% but only 40 sends
    assert not sender.is_paused(con)


def test_bounce_guard_pauses_over_limit(world):
    con = world.con
    pid = founder(con, name="B", email="b@x.ai")
    _fake_sends(con, pid, 60)
    db.add_event(con, pid, "bounce", "instantly")
    con.commit()
    with Run(con, "events") as r:
        assert events.bounce_guard(world, r) is False         # 1/60 = 1.7%
    db.add_event(con, pid, "bounce", "instantly")
    con.commit()
    with Run(con, "events") as r:
        assert events.bounce_guard(world, r) is True          # 2/60 = 3.3% > 2%
    assert sender.is_paused(con) and set(world.instantly.paused) >= {"camp-fnd"}
    assert "Sending paused" in (world.settings.data_dir / "alerts.log").read_text()


# ---------------- CRM ----------------
def test_crm_push_pull(world):
    lead = sent_one(world)
    lead["timestamp_last_contact"] = "2026-11-24T10:00:00Z"
    run_cmd(world, "events", events.poll_api)
    run_cmd(world, "crm", crm.push)
    tab = "CRM"
    pid = world.con.execute("SELECT person_id FROM drafts").fetchone()["person_id"]
    row = world.sheets.row_by_id(tab, pid)
    assert row[layout.CRM_COLUMNS.index("Status")] == "emailed"
    world.sheets.set_cell(tab, pid, "Booked", "2026-11-28")
    world.sheets.set_cell(tab, pid, "Owner", "Karishma")
    run_cmd(world, "crm", crm.pull)
    run_cmd(world, "crm", crm.pull)
    booked = world.con.execute("SELECT * FROM events WHERE type='booked'").fetchall()
    assert len(booked) == 1 and booked[0]["occurred_at"].startswith("2026-11-28")
    run_cmd(world, "crm", crm.push)
    row = world.sheets.row_by_id(tab, pid)
    assert row[layout.CRM_COLUMNS.index("Status")] == "booked"
    assert row[layout.CRM_COLUMNS.index("Owner")] == "Karishma"       # team column untouched


def test_suppression_tab_pull(world):
    world.sheets.tabs["Suppression"].append(["spam@x.com", "asked", "Karishma", "2026-11-01"])
    world.sheets.tabs["Suppression"].append(["@competitor.com", "", "", ""])
    assert crm.pull_suppression(world) == 2
    assert suppression.is_suppressed(world.con, "anyone@competitor.com")


# ---------------- forget ----------------
def test_forget_everywhere(world):
    lead = sent_one(world)
    run_cmd(world, "review", review.push)
    run_cmd(world, "crm", crm.push)
    res, _ = run_cmd(world, "forget", __import__("lead_engine.forget", fromlist=["forget"]).forget,
                     email="ADA0@northwind.ai")
    assert res["found"] and res["instantly_leads_deleted"] == 1
    con = world.con
    for table in ("persons", "drafts", "sends", "signals", "person_identifiers"):
        assert con.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] == 0, table
    assert suppression.is_suppressed(con, "ada0@northwind.ai")
    assert con.execute("SELECT value FROM suppression").fetchone()["value"] != "ada0@northwind.ai"
    assert world.instantly.blocked == ["ada0@northwind.ai"] and world.instantly.deleted == [lead["id"]]
    # re-import is blocked
    from lead_engine.sources import warm
    res2 = warm.import_rows(con, [{"email": "ada0@northwind.ai", "name": "Ada"}], "again", assume_us=True)
    assert res2.suppressed == 1


# ---------------- run-daily ----------------
def test_run_daily_stops_when_events_fail(world):
    approved(world)
    world.settings.data["instantly"]["mode"] = "csv"
    assert main(["run-daily"], ctx=world) == 1
    assert world.con.execute("SELECT COUNT(*) n FROM sends").fetchone()["n"] == 0


def test_run_daily_happy_path(world):
    approved(world)
    world.settings.data["sending"]["pilot_start"] = dt.date.today().isoformat()
    assert main(["run-daily"], ctx=world) == 0
    assert len(world.instantly.added) == 1
    assert world.sheets.row_by_id("CRM", world.con.execute("SELECT person_id FROM drafts").fetchone()["person_id"])


def test_draft_only_movers_flag(con):
    from lead_engine.drafting.drafter import eligible_people
    from conftest import make_person
    a = make_person(con, name="Ada Mover", segment="engineer", email="ada@x.io")
    make_person(con, name="Bob Stay", segment="engineer", email="bob@x.io")
    db.add_signal(con, a, "moved_to_us", "IN 2014 -> US 2019", "p")
    for pid, e in con.execute("SELECT person_id, email FROM persons").fetchall():
        db.add_identifier(con, pid, "email", e)
        db.touch_person(con, pid, email_status="verified", us_located=1)
    con.commit()
    assert len(eligible_people(con, "engineer", 10)) == 2
    assert [r["display_name"] for r in eligible_people(con, "engineer", 10, require_moved=True)] == ["Ada Mover"]


def test_pipeline_runs_what_its_keys_allow(ctx, monkeypatch, capsys):
    from lead_engine import commands
    from lead_engine.runtime import Run
    calls = []
    for name in ("cmd_source", "cmd_find_emails", "cmd_draft", "cmd_review"):
        monkeypatch.setattr(commands, name, lambda c, a, r, n=name: calls.append(
            (n, getattr(a, "group", None), getattr(a, "discover_only", None))) or 0)

    class A:
        groups, limit, budget, no_draft = ["engineer", "founder"], 5, None, False
    ctx.settings.data.setdefault("emails", {})["verifier"] = "reoon"
    ctx.settings.secrets.pop("VERIFIER_API_KEY", None)
    ctx.settings.secrets.pop("GEMINI_API_KEY", None)
    with Run(ctx.con, "pipeline") as r:
        assert commands.cmd_pipeline(ctx, A(), r) == 0
    assert calls == [("cmd_source", "engineer", None), ("cmd_find_emails", "engineer", True),
                     ("cmd_source", "founder", None), ("cmd_find_emails", "founder", True)]
    assert "skipping drafts" in capsys.readouterr().out
    calls.clear()
    ctx.settings.secrets.update({"VERIFIER_API_KEY": "k", "GEMINI_API_KEY": "g"})
    ctx.settings.data.setdefault("sheet", {})["spreadsheet_id"] = "s"
    with Run(ctx.con, "pipeline") as r:
        commands.cmd_pipeline(ctx, A(), r)
    assert [c[0] for c in calls] == ["cmd_source", "cmd_find_emails", "cmd_draft",
                                     "cmd_source", "cmd_find_emails", "cmd_draft", "cmd_review"]
    assert calls[1][2] is False
