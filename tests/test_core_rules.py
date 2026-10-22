import io

import pytest
from openpyxl import Workbook

from lead_engine import db, states, suppression
from lead_engine.drafting import checks
from lead_engine.emails import policy
from lead_engine.sources import warm

from conftest import make_person


# ---------------- suppression ----------------
def test_suppression_matches_email_domain_and_hash(con):
    suppression.add(con, "email", "a@x.com", "unsub", "instantly")
    suppression.add(con, "domain", "blocked.org", "manual", "manual")
    suppression.add(con, "email_hash", suppression.email_hash("Gone@Y.com"), "forget", "forget")
    assert suppression.is_suppressed(con, "A@X.com")
    assert suppression.is_suppressed(con, "anyone@blocked.org")
    assert suppression.is_suppressed(con, "gone@y.com")
    assert not suppression.is_suppressed(con, "b@x.com")
    assert not suppression.add(con, "email", "a@x.com", "again", "manual")  # idempotent


def test_normalize_and_role_addresses():
    assert suppression.normalize_email(" <Ada@Example.COM> ") == "ada@example.com"
    assert suppression.normalize_email("mailto:a@b.io") == "a@b.io"
    assert suppression.normalize_email("not an email") is None
    assert suppression.is_role_address("info@x.com") and not suppression.is_role_address("ada@x.com")


# ---------------- engineer rule ----------------
def test_engineer_never_at_corporate_or_employer_domain(con):
    pid = make_person(con, segment="engineer", employer_domain="smallco.io")
    p = db.get_person(con, pid)
    assert policy.violates_engineer_rule(p, "ada@google.com")
    assert policy.violates_engineer_rule(p, "ada@eng.google.com")
    assert policy.violates_engineer_rule(p, "ada@smallco.io")
    assert not policy.violates_engineer_rule(p, "ada@gmail.com")
    f = db.get_person(con, make_person(con, name="F", segment="founder", email="f@startup.ai"))
    assert not policy.violates_engineer_rule(f, "f@google.com")


def test_engineer_work_address_when_allowed(con, monkeypatch):
    monkeypatch.setattr(policy, "ENGINEER_WORK_OK", True)
    p = db.get_person(con, make_person(con, segment="engineer", employer_domain="google.com"))
    assert not policy.violates_engineer_rule(p, "ada@google.com")      # own employer: allowed
    assert policy.violates_engineer_rule(p, "ada@meta.com")            # another corporate domain: never
    assert policy.acceptable(p, "ada@google.com", "pattern_guess", "deliverable")[0]
    assert not policy.acceptable(p, "ada@google.com", "pattern_guess", "catch_all")[0]
    assert not policy.acceptable(p, "ada@google.com", "pattern_guess", "unknown")[0]


def test_acceptable_by_group(con):
    eng = db.get_person(con, make_person(con, segment="engineer"))
    assert policy.acceptable(eng, "ada@gmail.com", "published_page", "deliverable")[0]
    assert not policy.acceptable(eng, "ada@gmail.com", "hunter", "deliverable")[0]
    assert policy.acceptable(eng, "ada@ada.dev", "published_page", "catch_all")[0]
    fnd = db.get_person(con, make_person(con, name="F", segment="founder", email="f@co.ai"))
    assert not policy.acceptable(fnd, "f@co.ai", "hunter", "catch_all")[0]
    assert not policy.acceptable(fnd, "f@co.ai", "hunter", "unknown")[0]
    assert not policy.acceptable(fnd, "info@co.ai", "published_page", "deliverable")[0]


# ---------------- warm import ----------------
def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_us_located():
    assert warm.us_located("San Francisco, CA") == 1
    assert warm.us_located("Seattle") == 1
    assert warm.us_located("New York, NY, USA") == 1
    assert warm.us_located("Toronto, Canada") == 0
    assert warm.us_located("Bangalore, India") == 0
    assert warm.us_located("Remote") is None
    assert warm.us_located("") is None


def test_warm_import_maps_columns_dedupes_and_filters(con):
    content = _xlsx([
        ["Indoko signups export"],                                  # title row above the header
        ["Full Name", "Email Address", "Company", "Location", "Job Title"],
        ["Ada Lovelace", "ADA@lovelace.dev", "Analytical", "San Francisco, CA", "Engineer"],
        ["Ada L", "ada@lovelace.dev", "", "SF", ""],                # duplicate email
        ["Bad", "not-an-email", "", "", ""],
        ["Tor Onto", "tor@x.ca", "", "Toronto, Canada", ""],         # not US
        ["Remy", "remy@x.com", "", "", ""],                          # unknown location
        ["Sup", "sup@x.com", "", "Austin, TX", ""],
    ])
    suppression.add(con, "email", "sup@x.com", "unsub", "instantly")
    rows = warm.read_rows(content)
    res = warm.import_rows(con, rows, "signups", assume_us=False, us_only=True)
    assert (res.imported, res.existing, res.invalid_email, res.not_us, res.suppressed, res.unknown_location) == \
        (2, 1, 1, 1, 1, 1)
    p = con.execute("SELECT * FROM persons WHERE email='ada@lovelace.dev'").fetchone()
    assert p["segment"] == "warm" and p["us_located"] == 1 and p["title"] == "Engineer"
    remy = con.execute("SELECT us_located FROM persons WHERE email='remy@x.com'").fetchone()
    assert remy["us_located"] is None


def test_warm_import_assume_us(con):
    rows = warm.read_rows(_xlsx([["Name", "Email"], ["Remy", "remy@x.com"]]))
    warm.import_rows(con, rows, "list", assume_us=True)
    assert con.execute("SELECT us_located FROM persons").fetchone()["us_located"] == 1


def test_drive_files_require_consent(ctx):
    from lead_engine.runtime import Run

    ctx.settings.data["drive"] = {"warm_files": [{"id": "f1", "label": "old list"}]}
    with Run(ctx.con, "import-warm") as run:
        out = warm.import_drive_files(ctx, run)
    assert out == [] and run.skipped == 1


# ---------------- states ----------------
@pytest.mark.parametrize("decision,edits,expected", [
    ("Approve", {}, states.APPROVED),
    ("Approve", {"edited_body": "new"}, states.EDITED),
    ("Edit", {}, states.EDITED),
    ("", {"edited_fu2": "x"}, None),
    ("Reject", {"edited_body": "x"}, states.REJECTED),
    ("", {}, None),
])
def test_decide(decision, edits, expected):
    assert states.decide(decision, edits) == expected


def test_transitions():
    states.check_transition(states.PUSHED, states.APPROVED)
    states.check_transition(states.QUEUED, states.STALE)
    for bad in [(states.REJECTED, states.QUEUED), (states.PUSHED, states.QUEUED), (states.CLOSED, states.SENT)]:
        with pytest.raises(states.InvalidTransition):
            states.check_transition(*bad)


def test_final_text_prefers_edits():
    d = {"subject": "S", "body": "B", "fu1": "F1", "fu2": "F2", "edited_subject": "", "edited_body": "B2",
         "edited_fu1": None, "edited_fu2": "  "}
    assert states.final_text(d) == {"subject": "S", "body": "B2", "fu1": "F1", "fu2": "F2"}


def test_lead_status_precedence():
    assert states.lead_status([]) == "not contacted"
    assert states.lead_status(["sent", "reply", "positive_reply", "booked"]) == "booked"
    assert states.lead_status(["sent", "booked", "attended", "client"]) == "client"
    assert states.lead_status(["sent", "reply", "unsubscribe"]) == "unsubscribed"
    assert states.lead_status(["sent", "bounce"]) == "bounced"


# ---------------- checks ----------------
CTX = checks.CheckContext(
    evidence_texts=["Northwind raised a $4M seed round led by Acme Ventures in September 2026",
                    "Ada Lovelace spoke at PyCon US 2026 about Rust compilers"],
    allowlist=["Ada Lovelace", "Ada", "Karishma", "Indoko", "https://cal.com/indoko/x", "30-minute"])
FOOTER = 'Karishma, Indoko · 1 Market St\nThis is a message about Indoko\'s services. Reply "unsubscribe"...'


def test_grounded_accepts_evidence_and_normalized_numbers():
    ok, why = checks.check_grounded(["Congrats on the 4 million dollar seed from Acme Ventures",
                                     "Your PyCon US talk"], CTX)
    assert ok, why


def test_grounded_rejects_invented_names_and_numbers():
    ok, why = checks.check_grounded(["Saw your talk at Google Next about 12 compilers"], CTX)
    assert not ok and "Google Next" in why and "12" in why


def test_grounded_ignores_visa_names_and_links():
    ok, why = checks.check_grounded(["Options like the O-1, EB-1A or EB-2 NIW. Book: https://cal.com/indoko/x"], CTX)
    assert ok, why


@pytest.mark.parametrize("text", [
    "You'd likely qualify for the O-1.", "You are eligible for EB-1A.", "We guarantee results.",
    "Your profile is a perfect fit for the O-1A.", "You would be approved quickly.",
])
def test_eligibility_language_blocked(text):
    assert not checks.check_no_eligibility([text])[0]


def test_general_visa_mention_allowed():
    assert checks.check_no_eligibility(["Some founders look at options such as the O-1, EB-1A or EB-2 NIW."])[0]


@pytest.mark.parametrize("text", [
    "As an Indian engineer you may", "Given your visa status", "International students often",
    "your home country", "Many immigrants", "If you're on an H-1B",
])
def test_sensitive_blocked(text):
    assert not checks.check_no_sensitive([text])[0]


def test_sensitive_no_false_alarm_on_common_words():
    assert checks.check_no_sensitive(["You can opt in any time. The race to ship features."])[0]


def test_form_limits():
    long_body = "word " * 121
    assert not checks.check_form("Hi", long_body, [])[0]
    assert not checks.check_form("Re: your round", "Hi", [])[0]
    assert not checks.check_form("one two three four five six seven eight nine", "Hi", [])[0]
    assert not checks.check_form("Hi", "Book? Or call? ", [])[0]
    assert not checks.check_form("Hi", "a https://x.com b https://y.com", [])[0]
    assert checks.check_form("Your seed round", "Would a 30-minute call help? https://cal.com/x", ["short"])[0]


def test_footer_and_tone():
    assert not checks.check_footer(["fine"], "")[0]
    assert not checks.check_footer(["Reply unsubscribe to stop"], FOOTER)[0]
    assert checks.check_footer(["fine"], FOOTER)[0]
    assert not checks.check_tone(["I hope this email finds you well"])[0]
    assert not checks.check_tone(["Your amazing work"])[0]
    assert checks.check_tone(["Saw the round."])[0]


def test_engineer_guesses_skip_first_name_only(ctx, monkeypatch):
    from lead_engine.emails import finders
    monkeypatch.setattr(policy, "ENGINEER_WORK_OK", True)
    pid = make_person(ctx.con, name="Elliott Harris", segment="engineer", employer_domain="apple.com")
    got = [c["email"] for c in finders.FounderDomainFinder().find(ctx, db.get_person(ctx.con, pid), None)]
    assert got and "elliott.harris@apple.com" in got
    assert "elliott@apple.com" not in got and "eharris@apple.com" not in got



def test_foreign_domains_rejected():
    for e in ("li@wfxy.neu.edu.cn", "a@ox.ac.uk", "b@tum.de", "c@iitb.ac.in", "d@x.gov.au"):
        assert policy.foreign_domain(e), e
    for e in ("a@kodama.ai", "b@stripe.io", "c@ucdavis.edu", "d@noaa.gov", "e@electricplant.co", "f@gmail.com"):
        assert not policy.foreign_domain(e), e


@pytest.mark.parametrize("text,ok", [
    ("Your work on Siri's on-device models caught my eye.", True),
    ("After your years in India, scaling infra at Apple must feel different.", False),
    ("Moving from abroad to lead a team at Microsoft is a big step.", False),
    ("Since you came over from Russia, ...", False),
])
def test_drafts_never_mention_origin(text, ok):
    from lead_engine.drafting import checks
    assert checks.check_no_sensitive([text])[0] == ok
