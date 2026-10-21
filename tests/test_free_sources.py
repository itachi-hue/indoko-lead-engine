import datetime as dt

import pytest

from lead_engine import db
from lead_engine.runtime import Run
from lead_engine.sources import patents, sec_formd

INDEX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
D           NORTHWIND LABS INC                                            1990001     20261019    edgar/data/1990001/0001990001-26-000001.txt
D           ACME GROWTH FUND II LP                                        1990002     20261019    edgar/data/1990002/0001990002-26-000001.txt
D/A         OLDCO INC                                                     1990003     20261019    edgar/data/1990003/0001990003-26-000002.txt
10-K        BIGCO                                                         1990004     20261019    edgar/data/1990004/0001990004-26-000003.txt
"""


def form_d(name="NORTHWIND LABS INC", state="CA", industry="Other Technology", sold="4000000", amendment="false",
           people=(("ADA", "LOVELACE", "Executive Officer"), ("CHARLES", "BABBAGE", "Director"))):
    persons = "".join(
        f"<relatedPersonInfo><relatedPersonName><firstName>{f}</firstName><lastName>{l}</lastName></relatedPersonName>"
        f"<relatedPersonRelationshipList><relationship>{r}</relationship></relatedPersonRelationshipList></relatedPersonInfo>"
        for f, l, r in people)
    return f"""<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/formd">
<primaryIssuer><entityName>{name}</entityName><issuerAddress><city>SAN FRANCISCO</city>
<stateOrCountry>{state}</stateOrCountry></issuerAddress></primaryIssuer>
<relatedPersonsList>{persons}</relatedPersonsList>
<offeringData><industryGroup><industryGroupType>{industry}</industryGroupType></industryGroup>
<typeOfFiling><newOrAmendment><isAmendment>{amendment}</isAmendment></newOrAmendment>
<dateOfFirstSale><value>2026-10-01</value></dateOfFirstSale></typeOfFiling>
<offeringSalesAmounts><totalOfferingAmount>5000000</totalOfferingAmount><totalAmountSold>{sold}</totalAmountSold>
</offeringSalesAmounts></offeringData></edgarSubmission>"""


def test_daily_index_keeps_new_form_d_only():
    rows = sec_formd.parse_daily_index(INDEX)
    assert [r["company"] for r in rows] == ["NORTHWIND LABS INC", "ACME GROWTH FUND II LP"]
    assert sec_formd.primary_doc_url(rows[0]) == \
        "https://www.sec.gov/Archives/edgar/data/1990001/000199000126000001/primary_doc.xml"
    assert sec_formd.daily_index_url(dt.date(2026, 10, 19)).endswith("/2026/QTR4/form.20261019.idx")


def test_parse_and_filter_form_d():
    rec = sec_formd.parse_form_d(form_d())
    assert rec["company"] == "NORTHWIND LABS INC" and rec["amount_sold"] == 4_000_000 and rec["state"] == "CA"
    assert sec_formd.wanted(rec)
    assert not sec_formd.wanted(sec_formd.parse_form_d(form_d(industry="Pooled Investment Fund")))
    assert not sec_formd.wanted(sec_formd.parse_form_d(form_d(state="ON")))           # Canadian issuer
    assert not sec_formd.wanted(sec_formd.parse_form_d(form_d(sold="250000")))
    assert not sec_formd.wanted(sec_formd.parse_form_d(form_d(people=(("A", "B", "Director"),))))


def test_source_founders_from_edgar(ctx, router):
    ctx.settings.secrets["SEC_CONTACT_EMAIL"] = "ops@example.com"
    router.add("GET", r"daily-index/2026/QTR4/form\.20261019\.idx", INDEX)
    router.add("GET", r"daily-index", "", status=404)                                  # other days: holidays etc.
    router.add("GET", r"data/1990001/.*/primary_doc\.xml", form_d())
    router.add("GET", r"data/1990002/.*/primary_doc\.xml", form_d(name="ACME GROWTH FUND II LP",
                                                                    industry="Pooled Investment Fund"))
    router.add("GET", r"daily-index/2026/QTR4/form\.20261019\.idx", INDEX)
    with Run(ctx.con, "source") as r:
        n = sec_formd.source(ctx, r, 10, days_back=2, today=dt.date(2026, 10, 20))
    assert n == 1 and r.skipped == 1
    p = ctx.con.execute("SELECT * FROM persons").fetchone()
    assert (p["display_name"], p["primary_employer"], p["us_located"]) == ("Ada Lovelace", "Northwind Labs Inc", 1)
    ev = db.evidence_for(ctx.con, p["person_id"])[0]
    assert ev["evidence_text"] == "Northwind Labs Inc reported raising $4M in a private funding round (SEC Form D)"
    assert ev["evidence_date"] == "2026-10-19"
    assert all("Indoko lead engine ops@example.com" == c.headers["user-agent"] for c in router.calls)
    with Run(ctx.con, "source") as r:                                                    # filings are never re-read
        assert sec_formd.source(ctx, r, 10, days_back=2, today=dt.date(2026, 10, 20)) == 0


def test_sec_requires_contact(ctx):
    ctx.settings.secrets["SEC_CONTACT_EMAIL"] = ""
    with Run(ctx.con, "source") as r, pytest.raises(RuntimeError, match="SEC_CONTACT_EMAIL"):
        sec_formd.source(ctx, r, 1)


def _pat(pid, title, date, inventors):
    return {"patent_id": pid, "patent_title": title, "patent_date": date,
            "inventors": [{"inventor_name_first": f, "inventor_name_last": l, "inventor_city": c,
                           "inventor_state": s, "inventor_country": k} for f, l, c, s, k in inventors],
            "assignees": [{"assignee_organization": "Google LLC"}]}


def test_patents_grouping_and_ingest(ctx, router):
    ctx.settings.secrets["PATENTSVIEW_API_KEY"] = "k"
    ctx.settings.data.setdefault("sourcing", {})["moved_to_us"] = "off"
    pats = [_pat("12000001", "Distributed cache invalidation", "2026-05-01",
                 [("Ada", "Lovelace", "Mountain View", "CA", "US"), ("Hans", "Zimmer", "Zurich", None, "CH")]),
            _pat("12000002", "Low-latency RPC routing", "2026-07-01", [("Ada", "Lovelace", "Mountain View", "CA", "US")]),
            _pat("12000003", "One-off idea", "2026-08-01", [("Bob", "Builder", "Austin", "TX", "US")])]
    def handler(req):
        import httpx, json as _j
        org = _j.loads(req.content)["q"]["_and"][1]["_contains"]["assignees.assignee_organization"]
        return httpx.Response(200, json={"patents": pats if org == "Google" else []})
    router.add("POST", r"search.patentsview.org/api/v1/patent", handler=handler)
    with Run(ctx.con, "source") as r:
        n = patents.source(ctx, r, 3)
    assert n == 1                                    # Bob has 1 patent, Hans is not US-based
    body = router.body(router.calls_to("patentsview")[0])
    assert body["q"]["_and"][1]["_contains"]["assignees.assignee_organization"] == "Google"
    assert router.calls_to("patentsview")[0].headers["x-api-key"] == "k"
    p = ctx.con.execute("SELECT * FROM persons").fetchone()
    assert (p["display_name"], p["employer_domain"], p["us_located"]) == ("Ada Lovelace", "google.com", 1)
    ev = [e["evidence_text"] for e in db.evidence_for(ctx.con, p["person_id"])]
    assert ev == ['Patent: "Low-latency RPC routing"', 'Patent: "Distributed cache invalidation"']


def test_free_sources_are_the_default(ctx, monkeypatch):
    from lead_engine.sources import cold
    called = []
    monkeypatch.setattr(sec_formd, "source", lambda c, r, limit: called.append("formd") or 0)
    monkeypatch.setattr(patents, "source", lambda c, r, limit: called.append("patents") or 0)
    with Run(ctx.con, "source") as r:
        cold.source(ctx, r, "founder", 1)
        cold.source(ctx, r, "engineer", 1)
    assert called == ["formd", "patents"]


def test_engineer_companies_setting_limits_sources(ctx):
    from lead_engine.sources import cold
    ctx.settings.data.setdefault("sourcing", {})["engineer_companies"] = ["Apple", "Microsoft"]
    qs = cold._queries(ctx, "engineer")
    assert qs and all(" at Apple " in q or " at Microsoft " in q for q in qs)
