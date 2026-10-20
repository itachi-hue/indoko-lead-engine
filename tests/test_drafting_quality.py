"""The six checks measured against the 40 labelled drafts, plus the LinkedIn notes."""
import pytest

from lead_engine import db
from lead_engine.drafting import checks, linkedin
from lead_engine.runtime import Run

from conftest import make_person
from fakes import FakeGemini, FakeSheets
from lead_engine.eval.labelled_drafts import ALL, BAD, EV, GOOD, LINK, NAMES

FOOTER = 'Karishma, Indoko · 1 Market St\nThis is a message about Indoko\'s services. Reply "unsubscribe"...'


def run_checks(item):
    name = NAMES[item["segment"]]
    ctx = checks.CheckContext(evidence_texts=EV[item["segment"]],
                              allowlist=[name, name.split()[0], "Karishma", "Indoko", LINK, "30-minute 30 minutes"])
    return checks.run_all(item["subject"], item["body"], item["fu1"], item["fu2"], ctx, FOOTER)


def failed(item):
    return sorted(k for k, v in run_checks(item).items() if not v["pass"])


def test_labelled_set_size():
    assert len(GOOD) == 20 and len(BAD) == 20 and len(ALL) == 40


@pytest.mark.parametrize("item", BAD, ids=[b["id"] for b in BAD])
def test_every_banned_item_is_caught(item):
    """0 false negatives: each check the item should fail, fails."""
    got = failed(item)
    missing = [c for c in item["fails"] if c not in got]
    assert not missing, f"{item['id']} not caught by {missing}; reasons: {run_checks(item)}"


def test_false_alarm_rate_on_good_drafts():
    """At most 10% of good drafts trip any check."""
    alarms = {g["id"]: failed(g) for g in GOOD if failed(g)}
    assert len(alarms) <= 2, f"false alarms: {alarms}"


# ---------------- LinkedIn notes ----------------
def test_linkedin_notes(ctx):
    pid = make_person(ctx.con, segment="engineer", email=None)
    db.touch_person(ctx.con, pid, email_status="none")
    db.add_identifier(ctx.con, pid, "linkedin", "https://www.linkedin.com/in/ada")
    db.add_signal(ctx.con, pid, "patent", 1, "u1", evidence_text='Patent: "Distributed cache invalidation"',
                  evidence_date="2025-01-01", url="https://patents/u1")
    ctx.con.commit()
    ctx.gemini = FakeGemini([{"note": "Hi Ada, saw your patent on distributed cache invalidation. "
                                      "Would be glad to connect.", "evidence_ids": []}])
    ctx.sheets = FakeSheets()
    ctx.sheets.tabs["LinkedIn"] = [["person_id"]]
    with Run(ctx.con, "linkedin") as r:
        assert linkedin.prepare(ctx, r, 10) == 1
        assert linkedin.push(ctx, r) == 1
    row = ctx.sheets.tabs["LinkedIn"][1]
    assert row[0] == pid and row[3] == "https://linkedin.com/in/ada" and "cache invalidation" in row[5]
    assert ctx.con.execute("SELECT state FROM drafts WHERE channel='linkedin'").fetchone()["state"] == "pushed"
    with Run(ctx.con, "linkedin") as r:
        assert linkedin.prepare(ctx, r, 10) == 0          # never prepared twice


def test_linkedin_note_checks():
    cctx = checks.CheckContext(['Patent: "Distributed cache invalidation"'], ["Ada", "Ada Lovelace"])
    assert checks.passed(linkedin.note_checks("Hi Ada, saw your patent on cache invalidation.", cctx))
    assert not checks.passed(linkedin.note_checks("Hi Ada, see https://x.com", cctx))
    assert not checks.passed(linkedin.note_checks("Hi Ada, you qualify for the O-1!", cctx))
    assert not checks.passed(linkedin.note_checks("x" * 300, cctx))
