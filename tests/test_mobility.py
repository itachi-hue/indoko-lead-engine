import httpx
import json

from lead_engine import db
from lead_engine.runtime import Run
from lead_engine.sources import mobility, patents, researchers


def _author(aid, name, affs, lki_type="education"):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "works_count": 30, "cited_by_count": 900,
            "summary_stats": {"h_index": 12}, "counts_by_year": [{"year": 2026, "works_count": 3}],
            "last_known_institutions": [{"display_name": "Florida State University", "country_code": "US",
                                         "type": lki_type}],
            "affiliations": [{"institution": {"display_name": n, "country_code": c}, "years": ys} for n, c, ys in affs]}


def test_move_rules():
    moved = _author("A1", "M", [("Univ. of Florence", "IT", [2012, 2013, 2014]), ("FSU", "US", [2019, 2026])])
    assert mobility.researcher_move(moved, 10, 2026) == {"from_country": "IT", "from_place": "Univ. of Florence",
                                                          "from_year": 2012, "first_us_year": 2019}
    long_ago = _author("A2", "D", [("Belarus State", "BY", [1993]), ("NIST", "US", [2000, 2026])])
    assert mobility.researcher_move(long_ago, 10, 2026) is None          # moved 26 years ago
    us_start = _author("A3", "K", [("Michigan", "US", [1976]), ("Oxford", "GB", [2020])])
    assert mobility.researcher_move(us_start, 10, 2026) is None          # career started in the US
    assert mobility.researcher_move(_author("A4", "N", []), 10, 2026) is None
    pats = [{"date": "2014-03-01", "country": "IN", "city": "Bangalore"},
            {"date": "2021-06-01", "country": "US", "city": "Cupertino"}]
    assert mobility.inventor_move(pats, 10, 2026)["from_country"] == "IN"


def test_researchers_require_mover_and_skip_government(ctx, router):
    router.add("GET", r"api.openalex.org/authors\?filter=", {"results": [
        _author("A1", "Malbor Mover", [("Univ. of Florence", "IT", [2012]), ("FSU", "US", [2019, 2026])]),
        _author("A2", "Stay Home", [("Michigan", "US", [2005, 2026])]),
        _author("A3", "Fed Mover", [("Univ. of Florence", "IT", [2016]), ("NOAA", "US", [2020])], "government"),
    ]})
    router.add("GET", r"api.openalex.org/works\?filter=", {"results": []})
    with Run(ctx.con, "source") as r:
        assert researchers.source(ctx, r, 5) == 1
    p = ctx.con.execute("SELECT * FROM persons").fetchone()
    assert p["display_name"] == "Malbor Mover"
    s = ctx.con.execute("SELECT * FROM signals WHERE signal_type='moved_to_us'").fetchone()
    assert s["value"] == "IT 2012 -> US 2019" and s["evidence_text"] is None     # never citable in a draft
    assert all(e["signal_type"] != "moved_to_us" for e in db.evidence_for(ctx.con, p["person_id"]))


def test_patents_require_mover(ctx, router):
    ctx.settings.secrets["PATENTSVIEW_API_KEY"] = "k"
    ctx.settings.data.setdefault("sourcing", {})["engineer_companies"] = ["Apple"]
    recent = [{"patent_id": str(i), "patent_title": f"T{i}", "patent_date": f"2026-0{i}-01",
               "inventors": [{"inventor_name_first": f, "inventor_name_last": l, "inventor_city": "Cupertino",
                              "inventor_state": "CA", "inventor_country": "US"} for f, l in
                             (("Priya", "Rao"), ("Tom", "Smith"))]} for i in (1, 2)]
    history = {"Rao": [("2015-01-01", "IN", "Bangalore"), ("2021-01-01", "US", "Cupertino")],
               "Smith": [("2012-01-01", "US", "Austin")]}

    def handler(req):
        q = json.loads(req.content)["q"]["_and"]
        if "_gte" in q[0]:
            return httpx.Response(200, json={"patents": recent})
        last = q[1]["inventors.inventor_name_last"]
        first = q[0]["inventors.inventor_name_first"]
        return httpx.Response(200, json={"patents": [
            {"patent_date": d, "inventors": [{"inventor_name_first": first, "inventor_name_last": last,
                                              "inventor_city": c, "inventor_country": k}]}
            for d, k, c in history[last]]})
    router.add("POST", r"search.patentsview.org", handler=handler)
    with Run(ctx.con, "source") as r:
        assert patents.source(ctx, r, 5) == 1
    assert ctx.con.execute("SELECT display_name FROM persons").fetchone()[0] == "Priya Rao"


def test_long_quota_block_fails_fast_and_schools_fall_back(ctx, router):
    from lead_engine import http as h
    mobility._OPENALEX_BLOCKED["on"] = False
    slept = []
    router.add("GET", r"api.openalex.org/institutions", handler=lambda req: httpx.Response(
        429, headers={"Retry-After": "6260"}, json={}))
    assert mobility.school_country(ctx, "Technical University of Cluj-Napoca") == "abroad"   # from the name
    assert mobility._OPENALEX_BLOCKED["on"] is True
    assert len(router.calls_to("institutions")) == 1                     # no retries, no hour-long wait
    assert mobility.school_country(ctx, "University of Texas at Austin") == "US"
    assert len(router.calls_to("institutions")) == 1                     # breaker: not called again
    mobility._OPENALEX_BLOCKED["on"] = False
