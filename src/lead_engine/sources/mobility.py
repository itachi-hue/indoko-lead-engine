"""Moved-to-the-US signal: a career that started outside the US and moved here recently.

Built only from where someone worked or filed patents (OpenAlex affiliation history, patent
addresses), never from names, ethnicity or photos. It is a targeting signal, not evidence: it is
stored without evidence_text, so drafts can never cite it ("since you moved from ...").

sourcing.moved_to_us: "require" (default: only movers are added) | "prefer" (keep everyone,
record the signal) | "off".
sourcing.moved_within_years: how recent the first US year must be (default 10; someone who moved
decades ago is likely a citizen already).
"""
from __future__ import annotations

import datetime as dt

from .. import db


def mode(ctx) -> str:
    return (ctx.settings.get("sourcing.moved_to_us") or "require").lower()


def within_years(ctx) -> int:
    return int(ctx.settings.get("sourcing.moved_within_years", 10))


def _move(history: list[tuple[int, str, str]], within: int, today_year: int | None = None) -> dict | None:
    """history: (year, country_code, place). A move = the earliest years are all abroad, and the first
    US year is within `within` years of today."""
    hist = sorted((y, cc.upper(), place) for y, cc, place in history if y and cc)
    if not hist:
        return None
    start = hist[0][0]
    early = [h for h in hist if h[0] <= start + 2]
    if any(cc == "US" for _, cc, _ in early):
        return None                      # career started in the US
    us_years = [y for y, cc, _ in hist if cc == "US"]
    if not us_years:
        return None
    first_us = min(us_years)
    if first_us < (today_year or dt.date.today().year) - within:
        return None                      # moved long ago
    y, cc, place = early[0]
    return {"from_country": cc, "from_place": place, "from_year": y, "first_us_year": first_us}


def researcher_move(author: dict, within: int = 10, today_year: int | None = None) -> dict | None:
    """From an OpenAlex author: affiliations[].institution.country_code with years."""
    hist = []
    for af in author.get("affiliations") or []:
        inst = af.get("institution") or {}
        for y in af.get("years") or []:
            hist.append((y, inst.get("country_code") or "", inst.get("display_name") or ""))
    return _move(hist, within, today_year)


def inventor_move(patents: list[dict], within: int = 10, today_year: int | None = None) -> dict | None:
    """From an inventor's patents: [{date, country, city}]."""
    hist = [(int(str(p["date"])[:4]), p.get("country") or "", p.get("city") or "")
            for p in patents if p.get("date")]
    return _move(hist, within, today_year)


def record(con, person_id: str, move: dict, provenance: str) -> None:
    summary = f"{move['from_country']} {move['from_year']} -> US {move['first_us_year']}"
    db.add_signal(con, person_id, "moved_to_us", summary, provenance)   # no evidence_text: never quoted


# ---------------- profiles (Exa people entities: workHistory + educationHistory) ----------------
def _year(value) -> int | None:
    s = str(value or "")
    return int(s[:4]) if s[:4].isdigit() else None


def _school_cached(ctx, key: str):
    import json

    row = ctx.con.execute("SELECT payload FROM source_records WHERE source='openalex_institution' "
                          "AND source_native_id=?", (key,)).fetchone()
    return (json.loads(row["payload"]) or {}) if row else None


def _school_fetch(client, name: str) -> dict:
    import urllib.parse

    from .. import http

    res = http.get_json(client, "https://api.openalex.org/institutions?search="
                        + urllib.parse.quote(name) + "&per_page=1").get("results") or []
    return {"query": name, "country_code": res[0].get("country_code") if res else None,
            "matched": res[0].get("display_name") if res else None}


def school_country(ctx, name: str) -> str | None:
    """Country code of a school, from OpenAlex institutions (free). Cached in source_records."""
    from .. import http

    key = name.strip().lower()
    if not key:
        return None
    hit = _school_cached(ctx, key)
    if hit is not None:
        return hit.get("country_code")
    try:
        rec = _school_fetch(ctx.client, name)
    except http.HTTPError:
        return None              # unknown; not cached so a later run can retry
    db.upsert_source_record(ctx.con, "openalex_institution", key, "institution", rec)
    return rec["country_code"]


def prefetch_schools(ctx, profiles: list[dict], workers: int = 8) -> None:
    """Look up every uncached school in a batch of profiles in parallel, before the filter runs."""
    from .. import http

    names = {}
    for props in profiles:
        for ed in props.get("educationHistory") or []:
            nm = ((ed.get("institution") or {}).get("name") or "").strip()
            if nm and "high school" not in nm.lower() and _school_cached(ctx, nm.lower()) is None:
                names[nm.lower()] = nm
    if not names:
        return
    results = http.pmap(lambda nm: _school_fetch(ctx.client, nm), list(names.values()), workers)
    for key, rec in zip(names, results):
        if not isinstance(rec, Exception):
            db.upsert_source_record(ctx.con, "openalex_institution", key, "institution", rec)
    ctx.con.commit()


def profile_history(ctx, props: dict) -> list[tuple[int, str, str]]:
    """(year, country, place) from job locations and school countries. Unknown places are dropped."""
    from .warm import us_located

    hist = []
    for job in props.get("workHistory") or []:
        y = _year((job.get("dates") or {}).get("from"))
        where = us_located(job.get("location"))
        if y and where is not None:
            place = job.get("location") or ""
            country = "US" if where else (place.rsplit(",", 1)[-1].strip() or "abroad")
            hist.append((y, country, place))
    for ed in props.get("educationHistory") or []:
        y = _year((ed.get("dates") or {}).get("from"))
        school = ((ed.get("institution") or {}).get("name") or "").strip()
        if not y or not school or "high school" in school.lower():
            continue
        cc = school_country(ctx, school)
        if cc:
            hist.append((y, cc.upper(), school))
    return hist


def profile_move(ctx, props: dict, within: int, today_year: int | None = None) -> dict | None:
    return _move(profile_history(ctx, props), within, today_year)


def engineer_within_years(ctx) -> int:
    """Engineers: longer window than researchers, since green-card backlogs keep people on visas for years."""
    return int(ctx.settings.get("sourcing.engineer_moved_within_years", 15))
