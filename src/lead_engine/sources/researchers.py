"""Researchers from OpenAlex (free), with recent papers as citable evidence.

Ported from the o1-leadgen prototype's OpenAlex source. Differences: US-located authors anywhere
(not just at listed companies), achievement filters in the query, and per-paper evidence text.
"""
from __future__ import annotations

import logging
import urllib.parse

from .. import db, http
from ..identity import resolve_person
from . import mobility

log = logging.getLogger("lead_engine.researchers")
BASE = "https://api.openalex.org"

# OpenAlex field IDs (topics.field.id). Verify with a live call in M0 if a field returns nothing.
FIELDS = {
    "computer-science": "17", "engineering": "22", "physics": "31", "chemistry": "16",
    "materials": "25", "biology": "13", "medicine": "27", "neuroscience": "28", "mathematics": "26",
    "economics": "20",
}


def _mailto(settings) -> str:
    m = settings.secret("OPENALEX_MAILTO")
    return f"&mailto={urllib.parse.quote(m)}" if m else ""


def search_authors(ctx, limit: int, field: str | None = None, min_h: int = 8, max_h: int = 45,
                   min_works: int = 10, min_cites: int = 500, max_cites: int = 20000) -> list[dict]:
    """US-affiliated authors with a real record but not already famous.

    Live test (Oct 2026) showed why each filter is needed: sorting by citations alone returned
    Transformer authors with 100k+ citations; common names without ORCID were merged profiles.
    """
    filters = ["last_known_institutions.country_code:US", f"summary_stats.h_index:{min_h}-{max_h}",
               f"works_count:>{min_works}", f"cited_by_count:{min_cites}-{max_cites}", "has_orcid:true"]
    if field:
        filters.append(f"topics.field.id:{FIELDS.get(field, field)}")
    # A seeded random sample spreads results across the whole citation window; sorting by citations
    # bunched every result just under the cap. Same seed -> same people (reproducible).
    seed = int(ctx.settings.get("sourcing.researcher_seed", 42))
    url = (f"{BASE}/authors?filter={','.join(filters)}&sample={min(limit, 200)}&seed={seed}"
           f"&per_page={min(limit, 200)}{_mailto(ctx.settings)}")
    out = http.get_json(ctx.client, url).get("results", [])
    return out[:limit]


def recent_works(ctx, author_id: str, n: int = 3) -> list[dict]:
    aid = author_id.rsplit("/", 1)[-1]
    url = (f"{BASE}/works?filter=author.id:{aid},type:article|preprint|review|book-chapter"
           f"&sort=publication_date:desc&per_page={n}"
           f"&select=id,doi,title,publication_year,publication_date,cited_by_count,primary_location"
           f"{_mailto(ctx.settings)}")
    return http.get_json(ctx.client, url).get("results", [])


def recent_works_3y(a: dict) -> int:
    import datetime as dt

    floor = dt.date.today().year - 2
    return sum(c.get("works_count", 0) for c in (a.get("counts_by_year") or []) if (c.get("year") or 0) >= floor)


MIN_RECENT_WORKS = 2


def ingest_author(ctx, a: dict, works: list[dict]) -> str | None:
    con = ctx.con
    lki = (a.get("last_known_institutions") or [{}])[0] or {}
    if lki.get("country_code") != "US":
        return None
    if (lki.get("type") or "").lower() == "government":
        return None  # federal and state agencies: most of these jobs require citizenship
    oa = a["id"].rsplit("/", 1)[-1]
    ids = [("openalex", oa)]
    if a.get("orcid"):
        ids.append(("orcid", a["orcid"]))
    pid = resolve_person(con, ids, a.get("display_name") or "", segment="researcher", group_source="cold",
                         primary_employer=lki.get("display_name"), country="US", us_located=1,
                         title="Researcher")
    db.upsert_source_record(con, "openalex", oa, "person", {k: a.get(k) for k in
                            ("id", "display_name", "orcid", "works_count", "cited_by_count", "summary_stats")})
    prov = f"https://openalex.org/{oa}"
    ss = a.get("summary_stats") or {}
    db.add_signal(con, pid, "h_index", ss.get("h_index", 0), prov)
    db.add_signal(con, pid, "citations", a.get("cited_by_count", 0), prov,
                  evidence_text=f"{a.get('cited_by_count', 0):,} citations across {a.get('works_count', 0)} papers",
                  url=prov)
    db.add_signal(con, pid, "works_count", a.get("works_count", 0), prov)
    move = mobility.researcher_move(a, mobility.within_years(ctx))
    if move and mobility.mode(ctx) != "off":
        mobility.record(con, pid, move, prov)
    recent3 = recent_works_3y(a)
    db.add_signal(con, pid, "recent_works_3y", recent3, prov)
    for w in works:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        year = w.get("publication_year")
        text = f'Paper "{title}"' + (f" in {venue}" if venue else "") + (f" ({year})" if year else "")
        url = w.get("doi") or w.get("id")
        db.add_signal(con, pid, "paper", w.get("cited_by_count", 0), url, evidence_text=text,
                      evidence_date=w.get("publication_date"), url=url)
    return pid


def source(ctx, run, limit: int, field: str | None = None) -> int:
    require = mobility.mode(ctx) == "require"
    authors = search_authors(ctx, limit * (8 if require else 2), field)   # most careers start in the US
    added = 0
    for a in authors:
        if added >= limit:
            break
        if recent_works_3y(a) < MIN_RECENT_WORKS:
            run.skipped += 1
            continue
        if require and not mobility.researcher_move(a, mobility.within_years(ctx)):
            run.skipped += 1
            continue
        try:
            works = recent_works(ctx, a["id"])
            if ingest_author(ctx, a, works):
                added += 1
                run.ok += 1
        except http.HTTPError as exc:
            run.failed += 1
            log.warning("openalex author %s failed: HTTP %s", a.get("id"), exc.status)
    ctx.con.commit()
    return added
