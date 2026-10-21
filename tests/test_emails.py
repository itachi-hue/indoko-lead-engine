import pytest

from lead_engine import db, suppression
from lead_engine.emails import find, finders, policy, verify
from lead_engine.runtime import Run

from conftest import make_person


class StubVerifier:
    name, cost_usd = "stubv", 0.001

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def verify(self, email):
        self.calls.append(email)
        return self.results.get(email, "deliverable")


class StubFinder:
    def __init__(self, name, emails, cost=0.0):
        self.name, self.emails, self.cost_usd, self.calls = name, emails, cost, 0

    def find(self, ctx, person, run):
        self.calls += 1
        return [{"email": e, "source": self.name} for e in self.emails]


class FakeExaPages:
    def __init__(self, pages, search=None):
        self.pages, self.search_results = pages, search or []

    def contents(self, urls):
        return [p for p in self.pages if p["url"] in urls]

    def search(self, query, **kw):
        return self.search_results


def person(con, segment="founder", name="Ada Lovelace", **kw):
    pid = make_person(con, name=name, segment=segment, email=None, **kw)
    return pid


@pytest.fixture
def stubs(ctx, monkeypatch):
    v = StubVerifier()
    monkeypatch.setattr(verify, "build", lambda c: v)
    return v


def test_emails_in_text_and_obfuscation():
    text = "Contact: ada [at] lovelace [dot] dev or ADA@Lovelace.dev. Image foo@2x.png. info@lab.edu"
    assert finders.emails_in(text) == ["ada@lovelace.dev", "info@lab.edu"]


def test_name_matching():
    assert finders.name_matches("ada.lovelace@x.com", "Ada Lovelace")
    assert finders.name_matches("hjalitto@ucdavis.edu", "Henry J. Alitto")
    assert not finders.name_matches("ada.byron@x.com", "Ada Lovelace")      # shared first name only
    assert finders.name_matches("alovelace@x.com", "Ada Lovelace")
    assert not finders.name_matches("bob@x.com", "Ada Lovelace")


def test_published_page_finder(ctx, router):
    pid = person(ctx.con, "engineer")
    db.add_signal(ctx.con, pid, "personal_site", "https://ada.dev", "https://ada.dev")
    ctx.con.commit()
    router.add("GET", r"^https://ada\.dev", "<html><title>Ada</title><p>Ada Lovelace, engineer at Google. "
               "Write to me: hi [at] ada [dot] dev. info@ada.dev</p></html>")
    with Run(ctx.con, "x") as r:
        out = finders.PublishedPageFinder().find(ctx, db.get_person(ctx.con, pid), r)
    assert [c["email"] for c in out] == ["hi@ada.dev"]          # role address dropped


def test_published_page_requires_page_about_person(ctx, router):
    pid = person(ctx.con, "engineer")
    db.add_signal(ctx.con, pid, "personal_site", "https://x.dev", "https://x.dev")
    router.add("GET", r"^https://x\.dev", "<p>Bob Smith's site at Google. bob@x.dev</p>")
    with Run(ctx.con, "x") as r:
        assert finders.PublishedPageFinder().find(ctx, db.get_person(ctx.con, pid), r) == []


def test_find_uses_group_order_budget_and_ledger(ctx, stubs, monkeypatch):
    pid = person(ctx.con, "founder", employer="Northwind", employer_domain="northwind.ai")
    pub = StubFinder("published_page", [])
    hun = StubFinder("hunter", ["ada@northwind.ai"])
    monkeypatch.setattr(finders, "available", lambda c: {"published_page": pub, "hunter": hun})
    with Run(ctx.con, "find-emails") as r:
        res = find.find_emails(ctx, r, "founder", 10, 5.0)
    assert res["found"] == 1 and pub.calls == 1 and hun.calls == 1
    p = db.get_person(ctx.con, pid)
    assert (p["email"], p["email_source"], p["email_status"]) == ("ada@northwind.ai", "hunter", "verified")
    # second run: nobody left, and no repeated paid lookups
    with Run(ctx.con, "find-emails") as r:
        find.find_emails(ctx, r, "founder", 10, 5.0)
    assert hun.calls == 1


def test_engineers_never_use_finder_apis_or_work_domains(ctx, stubs, monkeypatch):
    pid = person(ctx.con, "engineer", employer="Google", employer_domain="google.com")
    pub = StubFinder("published_page", ["ada@google.com", "ada.lovelace@gmail.com"])
    hun = StubFinder("hunter", ["ada@personal.dev"])
    monkeypatch.setattr(finders, "available", lambda c: {"published_page": pub, "hunter": hun})
    with Run(ctx.con, "find-emails") as r:
        find.find_emails(ctx, r, "engineer", 10, 5.0)
    assert hun.calls == 0
    assert db.get_person(ctx.con, pid)["email"] == "ada.lovelace@gmail.com"
    assert "ada@google.com" not in stubs.calls                 # never even verified


def test_catch_all_only_when_published(ctx, monkeypatch):
    v = StubVerifier({"ada@northwind.ai": "catch_all"})
    monkeypatch.setattr(verify, "build", lambda c: v)
    pid = person(ctx.con, "founder", employer="Northwind", employer_domain="northwind.ai")
    monkeypatch.setattr(finders, "available", lambda c: {"hunter": StubFinder("hunter", ["ada@northwind.ai"])})
    with Run(ctx.con, "find-emails") as r:
        res = find.find_emails(ctx, r, "founder", 10, 5.0)
    assert res["found"] == 0 and db.get_person(ctx.con, pid)["email"] is None
    assert db.get_person(ctx.con, pid)["email_status"] == "none"


def test_suppressed_candidates_skipped(ctx, stubs, monkeypatch):
    person(ctx.con, "founder", employer_domain="northwind.ai")
    suppression.add(ctx.con, "domain", "northwind.ai", "asked", "manual")
    monkeypatch.setattr(finders, "available", lambda c: {"hunter": StubFinder("hunter", ["ada@northwind.ai"])})
    with Run(ctx.con, "find-emails") as r:
        assert find.find_emails(ctx, r, "founder", 10, 5.0)["found"] == 0
    assert stubs.calls == []


def test_budget_cap_stops(ctx, stubs, monkeypatch):
    for i in range(5):
        person(ctx.con, "founder", name=f"Ada L{i}", employer_domain="n.ai")
    icy = StubFinder("icypeas", [], cost=0.5)
    monkeypatch.setattr(finders, "available", lambda c: {"icypeas": icy})
    monkeypatch.setattr(finders, "GROUP_ORDER", {**finders.GROUP_ORDER, "founder": ["icypeas"]})
    with Run(ctx.con, "find-emails") as r:
        res = find.find_emails(ctx, r, "founder", 10, 1.0)
    assert icy.calls == 2 and res["cost_usd"] == 1.0          # stopped once $1.00 was spent


def test_no_verifier_refuses(ctx, monkeypatch):
    monkeypatch.setattr(verify, "build", lambda c: None)
    with Run(ctx.con, "find-emails") as r, pytest.raises(RuntimeError, match="verifier"):
        find.find_emails(ctx, r, "founder", 10, 5.0)


@pytest.mark.parametrize("cls,payload,expected", [
    (verify.MillionVerifier, {"result": "ok"}, "deliverable"),
    (verify.MillionVerifier, {"result": "catch_all"}, "catch_all"),
    (verify.MillionVerifier, {"result": "invalid"}, "undeliverable"),
    (verify.ZeroBounce, {"status": "valid"}, "deliverable"),
    (verify.ZeroBounce, {"status": "unknown"}, "unknown"),
    (verify.Reoon, {"status": "safe", "is_catch_all": False}, "deliverable"),
    (verify.Reoon, {"status": "safe", "is_catch_all": True}, "catch_all"),
    (verify.Reoon, {"status": "catch_all"}, "catch_all"),
    (verify.Reoon, {"status": "disabled"}, "undeliverable"),
    (verify.Reoon, {"status": "inbox_full"}, "unknown"),
])
def test_verifier_mapping(router, ctx, cls, payload, expected):
    router.add("GET", r"millionverifier|zerobounce|reoon", payload)
    assert cls(ctx.client, "k").verify("a@b.co") == expected



def test_engineer_page_must_name_employer(ctx, router):
    pid = person(ctx.con, "engineer", employer="Google")
    db.add_signal(ctx.con, pid, "personal_site", "https://chan.dev", "https://chan.dev")
    ctx.con.commit()
    router.add("GET", r"^https://chan\.dev", "<p>Ada Lovelace, designer in Berlin. ada@chan.dev</p>")
    with Run(ctx.con, "x") as r:
        assert finders.PublishedPageFinder().find(ctx, db.get_person(ctx.con, pid), r) == []


def test_discover_only_marks_nobody_and_reuses_lookups(ctx, monkeypatch):
    pid = person(ctx.con, "founder", employer_domain="northwind.ai")
    hun = StubFinder("hunter", ["ada@northwind.ai"])
    monkeypatch.setattr(finders, "available", lambda c: {"hunter": hun})
    monkeypatch.setattr(verify, "build", lambda c: None)
    with Run(ctx.con, "find-emails") as r:
        res = find.find_emails(ctx, r, "founder", 10, 5.0, discover_only=True)
    assert res["unverified_candidates"]["Ada Lovelace"][0]["email"] == "ada@northwind.ai"
    assert db.get_person(ctx.con, pid)["email"] is None                   # not usable until verified
    v = StubVerifier()
    monkeypatch.setattr(verify, "build", lambda c: v)
    with Run(ctx.con, "find-emails") as r:
        res = find.find_emails(ctx, r, "founder", 10, 5.0)
    assert res["found"] == 1 and hun.calls == 1 and v.calls == ["ada@northwind.ai"]   # lookup not repeated



def test_founder_domain_and_patterns(ctx, router):
    from lead_engine.emails import domains
    pid = person(ctx.con, "founder", name="Justin Mast", employer="Electric Plant Company", employer_domain=None)
    db.add_signal(ctx.con, pid, "funding", "1", "u", evidence_text="Bombadil plant signals model, Bombadil sensors",
                  evidence_date="2026-10-20", url="https://news/x")
    ctx.con.commit()
    router.add("GET", r"^https://electricplant\.co/?$", "<title>Electric Plant</title><p>Electric Plant: Bombadil reads plant signals from sensors</p>")
    router.add("GET", r"^https://electricplantcompany\.com", "<title>Parked domain</title>")
    with Run(ctx.con, "x") as r:
        out = finders.FounderDomainFinder().find(ctx, db.get_person(ctx.con, pid), r)
    assert db.get_person(ctx.con, pid)["employer_domain"] == "electricplant.co"
    assert [c["email"] for c in out][:3] == ["justin@electricplant.co", "justin.mast@electricplant.co",
                                             "justinmast@electricplant.co"]
    assert {c["source"] for c in out} == {"pattern_guess"}


def test_founder_published_address_preferred(ctx, router):
    pid = person(ctx.con, "founder", name="Ada Lovelace", employer="Northwind", employer_domain="northwind.ai")
    router.add("GET", r"^https://northwind\.ai/team", '<a href="mailto:ada@northwind.ai">Ada Lovelace, CEO</a>')
    with Run(ctx.con, "x") as r:
        out = finders.FounderDomainFinder().find(ctx, db.get_person(ctx.con, pid), r)
    assert out == [{"email": "ada@northwind.ai", "source": "published_page", "url": "https://northwind.ai/team"}]


def test_guesses_need_a_definite_mailbox(ctx):
    f = db.get_person(ctx.con, person(ctx.con, "founder"))
    r = db.get_person(ctx.con, person(ctx.con, "researcher", name="Grace Hopper"))
    assert policy.acceptable(f, "ada@northwind.ai", "pattern_guess", "deliverable")[0]
    assert not policy.acceptable(f, "ada@northwind.ai", "pattern_guess", "catch_all")[0]
    assert not policy.acceptable(r, "grace@uni.edu", "pattern_guess", "deliverable")[0]



def test_patterns_handle_apostrophes():
    from lead_engine.emails import domains
    assert domains.pattern_candidates("Justin D’Souza", "x.ai")[:2] == ["justin@x.ai", "justin.dsouza@x.ai"]


def test_domain_needs_company_specific_word(ctx, router):
    from lead_engine.emails import domains
    router.add("GET", r"^https://kodama\.com/?$", "<title>Kodama | Restaurant</title><p>Kodama sushi forestry</p>")
    router.add("GET", r"^https://kodama\.ai/?$", "<title>Home</title><p>Kodama: autonomy for logging skidders</p>")
    router.add("GET", r"^https://getkodama\.com/?$", "<title>Kodama carbon</title><p>Kodama intelligent API</p>")
    kws = ["kodama", "logging", "skidders", "forestry", "intelligent", "operators"]
    assert domains.company_domain(ctx.client, "Kodama", kws) == "kodama.ai"
    assert domains.company_domain(ctx.client, "Kodama", ["intelligent"]) is None      # one shared word is not enough
