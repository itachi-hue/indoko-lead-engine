import re

import pytest

from lead_engine import db, scoring
from lead_engine.runtime import Run
from lead_engine.sources import cold, extract, researchers

from fakes import FakeGemini


class FakeExa:
    """search() returns the first registered result list whose pattern matches the query."""

    def __init__(self):
        self.routes: list[tuple[re.Pattern, list]] = []
        self.queries: list[tuple[str, dict]] = []

    def on(self, pattern, results):
        self.routes.append((re.compile(pattern, re.I), results))
        return self

    def search(self, query, **kw):
        self.queries.append((query, kw))
        for pat, res in self.routes:
            if pat.search(query):
                return res
        return []


ARTICLE = ("SAN FRANCISCO — Northwind, a San Francisco-based developer tools startup, has raised $4M in a seed "
           "round led by Acme Ventures. The company was founded by Ada Lovelace and Charles Babbage in 2025.")


@pytest.fixture
def sctx(ctx):
    ctx.exa = FakeExa()
    ctx.gemini = FakeGemini()
    return ctx


# ---------------- extraction verification ----------------
def test_verify_founder_drops_invented_values():
    raw = {"company": "Northwind", "round": "seed", "amount": "$5M", "location": "San Francisco",
           "founders": [{"name": "Ada Lovelace", "title": "CEO"}, {"name": "Grace Hopper"}, {"name": "Ada"}]}
    rec = extract.verify_founder_record(raw, ARTICLE)
    assert rec["company"] == "Northwind" and rec["round"] == "seed"
    assert "amount" not in rec                                   # $5M is not in the article
    assert [f["name"] for f in rec["founders"]] == ["Ada Lovelace"]


def test_verify_founder_rejects_unknown_company():
    assert extract.verify_founder_record({"company": "Southwind", "founders": [{"name": "Ada Lovelace"}]},
                                         ARTICLE) is None


def test_verify_creators():
    text = "Maya Chen, a Brooklyn-based filmmaker, won the Grand Jury Prize at Sundance for her debut feature."
    out = extract.verify_creator_records({"people": [
        {"name": "Maya Chen", "achievement": "won the Grand Jury Prize at Sundance", "location": "Brooklyn"},
        {"name": "Tom Fake", "achievement": "won an Oscar"}]}, text)
    assert out == [{"name": "Maya Chen", "achievement": "won the Grand Jury Prize at Sundance",
                    "location": "Brooklyn"}]


# ---------------- engineers ----------------
def test_parse_profile():
    p = cold.parse_profile({"title": "Ada Lovelace - Staff Software Engineer - Google | LinkedIn",
                            "url": "https://www.linkedin.com/in/ada", "text": "San Francisco Bay Area"})
    assert (p["name"], p["company"]) == ("Ada Lovelace", "Google") and "Staff" in p["headline"]
    assert cold.parse_profile({"title": "Jobs at Google"}) is None


def test_source_engineers_with_evidence(sctx):
    sctx.exa.on(r"patent", [
        {"title": "Distributed cache invalidation - US Patent 11,234,567", "url": "https://patents.google.com/p1",
         "publishedDate": "2025-03-01", "text": "Inventors: Ada Lovelace, Mountain View. Assignee: Google LLC"},
        {"title": "Unrelated patent", "url": "https://patents.google.com/p2", "text": "Inventor: John Smith"}])
    sctx.exa.on(r"talk", [{"title": "Scaling caches at Google", "url": "https://conf/talk",
                           "publishedDate": "2025-06-01", "text": "Speaker: Ada Lovelace, Staff Engineer, Google"}])
    sctx.exa.on(r"personal website", [{"title": "Ada Lovelace", "url": "https://ada.dev"}])
    sctx.exa.on(r"engineer at", [
        {"title": "Ada Lovelace - Staff Software Engineer - Google | LinkedIn",
         "url": "https://www.linkedin.com/in/ada", "text": "San Francisco Bay Area · 500+ connections"},
        {"title": "Bob Builder - Software Engineer - Google | LinkedIn", "url": "https://www.linkedin.com/in/bob",
         "text": "Seattle"},                                       # not senior -> skipped
    ])
    sctx.settings.data["sourcing"] = {"queries": {"engineer": ["staff software engineer at Google"]}}
    with Run(sctx.con, "source") as r:
        n = cold.source(sctx, r, "engineer", 5)
    assert n == 1 and r.skipped == 1 and r.cost_usd > 0
    p = sctx.con.execute("SELECT * FROM persons").fetchone()
    assert (p["employer_domain"], p["us_located"]) == ("google.com", 1)
    ev = [s["evidence_text"] for s in db.evidence_for(sctx.con, p["person_id"])]
    assert len(ev) == 2 and any("Patent" in e for e in ev) and all("John Smith" not in e for e in ev)
    site = sctx.con.execute("SELECT value FROM signals WHERE signal_type='personal_site'").fetchone()
    assert site["value"] == "https://ada.dev"


# ---------------- founders and creators ----------------
def test_source_founders(sctx):
    sctx.exa.on(r"raises", [{"title": "Northwind raises $4M", "url": "https://news/nw",
                             "publishedDate": "2026-09-10T00:00:00Z", "text": ARTICLE}])
    sctx.gemini.outputs = [{"company": "Northwind", "round": "seed", "amount": "$4M", "location": "San Francisco",
                            "founders": [{"name": "Ada Lovelace", "title": "CEO"},
                                         {"name": "Charles Babbage", "title": "CTO"}]}]
    sctx.settings.data["sourcing"] = {"queries": {"founder": ["startup raises seed round"]}}
    with Run(sctx.con, "source") as r:
        assert cold.source(sctx, r, "founder", 10) == 2
    rows = sctx.con.execute("SELECT * FROM signals WHERE signal_type='funding'").fetchall()
    assert {r["evidence_text"] for r in rows} == {"Northwind raised $4M in a seed round"}
    assert all(p["us_located"] == 1 for p in sctx.con.execute("SELECT us_located FROM persons"))
    # running again does not duplicate people
    sctx.gemini.outputs = [dict(sctx.gemini.outputs or {}, **{"company": "Northwind", "round": "seed",
                               "founders": [{"name": "Ada Lovelace"}]})]
    with Run(sctx.con, "source") as r:
        cold.source(sctx, r, "founder", 10)
    assert sctx.con.execute("SELECT COUNT(*) n FROM persons").fetchone()["n"] == 2


def test_source_creators(sctx):
    text = "Maya Chen, a Brooklyn-based filmmaker, won the Grand Jury Prize at Sundance for her debut feature."
    sctx.exa.on(r"filmmaker", [{"title": "Sundance winners", "url": "https://www.variety.com/x",
                                "publishedDate": "2026-02-01", "text": text}])
    sctx.gemini.outputs = [{"people": [{"name": "Maya Chen", "discipline": "filmmaker",
                                        "achievement": "won the Grand Jury Prize at Sundance", "location": "Brooklyn"}]}]
    sctx.settings.data["sourcing"] = {"queries": {"creator": ["filmmaker wins festival award"]}}
    with Run(sctx.con, "source") as r:
        assert cold.source(sctx, r, "creator", 5) == 1
    s = sctx.con.execute("SELECT * FROM signals WHERE signal_type='press'").fetchone()
    assert s["value"] == "variety.com" and s["evidence_text"] == "won the Grand Jury Prize at Sundance"


# ---------------- researchers ----------------
def test_source_researchers(ctx, router):
    router.add("GET", r"api.openalex.org/authors\?filter=last_known_institutions.country_code:US", {
        "results": [
            {"id": "https://openalex.org/A1", "display_name": "Grace Hopper", "orcid": None,
             "works_count": 40, "cited_by_count": 2500, "summary_stats": {"h_index": 20},
             "counts_by_year": [{"year": 2026, "works_count": 3}, {"year": 2025, "works_count": 4}],
             "last_known_institutions": [{"display_name": "Stanford University", "country_code": "US"}]},
            {"id": "https://openalex.org/A2", "display_name": "Abroad Person", "works_count": 40,
             "cited_by_count": 900, "summary_stats": {"h_index": 12},
             "last_known_institutions": [{"display_name": "Oxford", "country_code": "GB"}]},
        ], "meta": {"next_cursor": None}})
    router.add("GET", r"api.openalex.org/works\?filter=author.id:A1", {"results": [
        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x", "title": "Compilers at scale",
         "publication_year": 2026, "publication_date": "2026-05-01", "cited_by_count": 12,
         "primary_location": {"source": {"display_name": "PLDI"}}}]})
    router.add("GET", r"api.openalex.org/works\?filter=author.id:A2", {"results": []})
    with Run(ctx.con, "source") as r:
        assert researchers.source(ctx, r, 5, "computer-science") == 1
    url = str(router.calls_to(r"/authors")[0].url)
    assert "topics.field.id:17" in url and "summary_stats.h_index:8-45" in url
    ev = [e["evidence_text"] for e in db.evidence_for(ctx.con, ctx.con.execute("SELECT person_id FROM persons").fetchone()[0])]
    assert 'Paper "Compilers at scale" in PLDI (2026)' in ev


# ---------------- scoring ----------------
def test_scoring_achievement_only_and_percentiles(con):
    a = db.create_person(con, "Ada A", segment="engineer", title="Principal Engineer", us_located=1)
    b = db.create_person(con, "Bob B", segment="engineer", title="Software Engineer", us_located=1)
    for i in range(3):
        db.add_signal(con, a, "patent", 1, f"u{i}", evidence_text=f"Patent {i}", evidence_date="2025-01-01", url=f"u{i}")
    f = db.create_person(con, "Fay F", segment="founder", us_located=1)
    db.add_signal(con, f, "funding", "$20M", "n1", evidence_text="Acme raised $20M in a Series A round",
                  evidence_date="2026-09-01", url="n1")
    counts = scoring.score_all(con)
    assert counts == {"engineer": 2, "founder": 1}
    pa, pb = (con.execute("SELECT percentile FROM scores WHERE person_id=?", (x,)).fetchone()[0] for x in (a, b))
    assert pa > pb
    sub = con.execute("SELECT subscores FROM scores WHERE person_id=?", (f,)).fetchone()[0]
    assert '"stage": 0.75' in sub
    assert not any(k in sub for k in ("immigration", "sponsorship", "remuneration", "employer"))
    assert scoring.evidence_ok(con, a) and not scoring.evidence_ok(con, f)


def test_outside_extractions_are_still_verified(sctx):
    arts = [{"url": "https://news/nw", "title": "Northwind raises", "publishedDate": "2026-09-10", "text": ARTICLE},
            {"url": "https://news/other", "title": "x", "publishedDate": "2026-09-10", "text": "Nothing here."}]
    extracted = {
        "https://news/nw": {"company": "Northwind", "round": "seed", "amount": "$9M",       # $9M not in article
                            "founders": [{"name": "Ada Lovelace"}, {"name": "Invented Person"}]},
        "https://news/other": {"company": "Ghost Inc", "founders": [{"name": "No One"}]},
    }
    with Run(sctx.con, "source") as r:
        n = cold.ingest_articles(sctx, r, "founder", arts, 10, extracted)
    assert n == 1 and r.skipped == 1
    fact = sctx.con.execute("SELECT evidence_text FROM signals WHERE signal_type='funding'").fetchone()[0]
    assert fact == "Northwind raised funding in a seed round"            # unverified amount dropped
    assert sctx.gemini.prompts == []                                      # Gemini never called
