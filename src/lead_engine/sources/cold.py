"""Cold sourcing through Exa: senior engineers, founders, and creators (docs/DESIGN.md section 3).

Engineers come from Exa people search, then a few targeted searches for evidence (patents,
talks, papers). Founders and creators come from news, with Gemini extracting the people and
every extracted value verified against the article text (sources/extract.py).
Evidence counts only if it names the person.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse

from .. import db, http
from ..drafting.gemini import GeminiError
from ..identity import resolve_person
from . import extract
from .exa import CONTENTS_COST_USD, SEARCH_COST_USD
from .warm import us_located

log = logging.getLogger("lead_engine.cold")
EXTRACT_COST_USD = 0.001  # rough per-article Gemini Flash cost

COMPANIES = {  # name -> primary domain (used for the engineer domain rule)
    "Google": "google.com", "Meta": "meta.com", "Apple": "apple.com", "Amazon": "amazon.com",
    "Microsoft": "microsoft.com", "Netflix": "netflix.com", "NVIDIA": "nvidia.com", "OpenAI": "openai.com",
    "Stripe": "stripe.com", "Databricks": "databricks.com", "Uber": "uber.com", "Airbnb": "airbnb.com",
    "LinkedIn": "linkedin.com", "Salesforce": "salesforce.com", "Tesla": "tesla.com",
}
DEFAULT_QUERIES = {
    "engineer": [f"{lvl} software engineer at {c} in the United States"
                 for c in ("Google", "Meta", "Apple", "Amazon", "Microsoft", "Netflix", "NVIDIA")
                 for lvl in ("staff", "principal")],
    "founder": ["startup raises seed round", "startup raises Series A", "Y Combinator startup raises funding",
                "founders announce funding round for AI startup"],
    "creator": ["filmmaker wins festival award", "musician wins award", "artist announces solo exhibition",
                "photographer wins award", "documentary premieres at film festival", "designer wins award"],
}
SENIOR_TITLE = re.compile(r"\b(staff|principal|distinguished|senior staff|fellow|director|"
                          r"head of|vp|lead)\b", re.I)


def _queries(ctx, group: str) -> list[str]:
    return list(ctx.settings.get(f"sourcing.queries.{group}", []) or DEFAULT_QUERIES[group])


def _search(ctx, run, query: str, **kw) -> list[dict]:
    results = ctx.exa.search(query, **kw)
    run.cost_usd += SEARCH_COST_USD + (CONTENTS_COST_USD * len(results) if kw.get("include_text") else 0)
    return results


def _names_person(name: str, text: str) -> bool:
    parts = [p for p in re.split(r"\s+", name.strip()) if len(p) > 1]
    if len(parts) < 2:
        return False
    low = text.lower()
    return parts[0].lower() in low and parts[-1].lower() in low


# ---------------- engineers ----------------
def parse_profile(result: dict) -> dict | None:
    """'Ada Lovelace - Staff Software Engineer - Google | LinkedIn' -> name, headline, company."""
    title = (result.get("title") or "").replace(" | LinkedIn", "")
    parts = [p.strip() for p in re.split(r"\s[-–|]\s", title) if p.strip()]
    if len(parts) < 2 or len(parts[0]) > 60:
        return None
    name_tokens = parts[0].split()
    if not 2 <= len(name_tokens) <= 4 or not all(t[:1].isupper() for t in name_tokens):
        return None
    name, headline = parts[0], " · ".join(parts[1:])
    blob = f"{headline} {(result.get('text') or '')[:600]}"
    company = next((c for c in COMPANIES if re.search(rf"\b{re.escape(c)}\b", blob)), None)
    return {"name": name, "headline": headline, "company": company, "url": result.get("url", ""),
            "location_hint": (result.get("text") or "")[:300]}


def _add_evidence(ctx, run, pid: str, name: str, company: str | None, kind: str, query: str) -> int:
    added = 0
    for r in _search(ctx, run, query, num_results=5, include_text=True):
        text = f"{r.get('title', '')} {(r.get('text') or '')[:3000]}"
        if not _names_person(name, text):
            continue
        if kind == "talk" and company and company.lower() not in text.lower():
            continue  # talks are noisy; require the employer too
        title = (r.get("title") or "").strip()[:160]
        date = (r.get("publishedDate") or "")[:10] or None
        label = {"patent": "Patent", "talk": "Talk", "paper": "Paper", "project": "Open-source project"}[kind]
        db.add_signal(ctx.con, pid, kind, 1, r.get("url"), evidence_text=f'{label}: "{title}"',
                      evidence_date=date, url=r.get("url"))
        added += 1
        if added >= 2:
            break
    return added


def _add_personal_site(ctx, run, pid: str, name: str) -> None:
    for r in _search(ctx, run, f"{name} personal website", num_results=3, category="personal site"):
        if _names_person(name, f"{r.get('title', '')} {r.get('url', '')}"):
            db.add_signal(ctx.con, pid, "personal_site", r.get("url"), r.get("url"))
            return


def source_engineers(ctx, run, limit: int) -> int:
    category = ctx.settings.get("sourcing.people_category", "people")
    added = 0
    for q in _queries(ctx, "engineer"):
        if added >= limit:
            break
        for r in _search(ctx, run, q, num_results=10, category=category, include_text=True):
            if added >= limit:
                break
            prof = parse_profile(r)
            if not prof or not prof["company"] or not SENIOR_TITLE.search(prof["headline"]):
                run.skipped += 1
                continue
            loc = us_located(prof["location_hint"])
            id_type = "linkedin" if "linkedin.com/in/" in prof["url"] else "exa"
            pid = resolve_person(ctx.con, [(id_type, prof["url"])], prof["name"], segment="engineer",
                                 group_source="cold", primary_employer=prof["company"],
                                 employer_domain=COMPANIES[prof["company"]], title=prof["headline"][:120],
                                 us_located=loc, country="US" if loc == 1 else None)
            db.upsert_source_record(ctx.con, "exa_people", prof["url"], "person", prof)
            n = _add_evidence(ctx, run, pid, prof["name"], prof["company"], "patent",
                              f'"{prof["name"]}" patent {prof["company"]}')
            n += _add_evidence(ctx, run, pid, prof["name"], prof["company"], "talk",
                               f'"{prof["name"]}" conference talk')
            if n == 0:
                n += _add_evidence(ctx, run, pid, prof["name"], prof["company"], "paper",
                                   f'"{prof["name"]}" research paper')
            _add_personal_site(ctx, run, pid, prof["name"])
            ctx.con.commit()
            added += 1
            run.ok += 1
    return added


# ---------------- founders and creators (news + verified extraction) ----------------
def _since(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat() + "T00:00:00.000Z"


def _key(*parts: str) -> str:
    return "|".join(re.sub(r"\s+", " ", p.strip().lower()) for p in parts)


ARTICLE_GROUPS = {
    "founder": (extract.FOUNDER_SYSTEM, extract.FOUNDER_SCHEMA, 180),
    "creator": (extract.CREATOR_SYSTEM, extract.CREATOR_SCHEMA, 365),
}


def fetch_articles(ctx, run, group: str, max_articles: int) -> list[dict]:
    """Recent news articles for founders or creators (Exa news search, with text)."""
    days = ARTICLE_GROUPS[group][2]
    out, seen = [], set()
    for q in _queries(ctx, group):
        if len(out) >= max_articles:
            break
        for art in _search(ctx, run, q, num_results=10, category="news", include_text=True,
                           start_published_date=_since(days)):
            url = art.get("url")
            if not url or url in seen or not art.get("text"):
                continue
            seen.add(url)
            out.append({k: art.get(k) for k in ("url", "title", "publishedDate", "text")})
    return out[:max_articles]


def _ingest_founder(ctx, run, art: dict, raw: dict, room: int) -> int:
    text = art.get("text") or ""
    rec = extract.verify_founder_record(raw or {}, text)
    if not rec:
        run.skipped += 1
        return 0
    loc = us_located(rec.get("location")) if rec.get("location") else us_located(text[:1500])
    fact = f"{rec['company']} raised" + (f" {rec['amount']}" if rec.get("amount") else " funding") + \
        (f" in a {rec['round']} round" if rec.get("round") else "")
    added = 0
    for f in rec["founders"][:room]:
        pid = resolve_person(ctx.con, [("founder_key", _key(f["name"], rec["company"]))], f["name"],
                             segment="founder", group_source="cold", primary_employer=rec["company"],
                             title=(f.get("title") or "Founder")[:80], us_located=loc,
                             country=rec.get("location"))
        db.add_signal(ctx.con, pid, "funding", rec.get("amount") or "", art.get("url"),
                      evidence_text=fact, evidence_date=(art.get("publishedDate") or "")[:10] or None,
                      url=art.get("url"))
        db.upsert_source_record(ctx.con, "exa_news", art.get("url", ""), "article",
                                {"title": art.get("title"), "url": art.get("url"), "record": rec})
        added += 1
    return added


def _ingest_creator(ctx, run, art: dict, raw: dict, room: int) -> int:
    text = art.get("text") or ""
    recs = extract.verify_creator_records(raw or {}, text)
    if not recs:
        run.skipped += 1
        return 0
    added = 0
    for rec in recs[:room]:
        loc = us_located(rec.get("location"))
        pid = resolve_person(ctx.con, [("creator_key", _key(rec["name"], rec.get("discipline", "")))],
                             rec["name"], segment="creator", group_source="cold",
                             title=rec.get("discipline"), us_located=loc, country=rec.get("location"))
        outlet = urllib.parse.urlparse(art.get("url", "")).netloc.removeprefix("www.")
        db.add_signal(ctx.con, pid, "press", outlet, art.get("url"), evidence_text=rec["achievement"],
                      evidence_date=(art.get("publishedDate") or "")[:10] or None, url=art.get("url"))
        _add_personal_site(ctx, run, pid, rec["name"])
        added += 1
    return added


def ingest_articles(ctx, run, group: str, articles: list[dict], limit: int,
                    extracted: dict[str, dict] | None = None) -> int:
    """Extract people from each article and import only what the article text supports.

    `extracted` maps article URL -> extraction JSON prepared outside the pipeline (e.g. by Claude in a
    session). Without it, Gemini extracts. Either way the same verification applies.
    """
    system, schema, _ = ARTICLE_GROUPS[group]
    ingest = _ingest_founder if group == "founder" else _ingest_creator
    added = 0
    for art in articles:
        if added >= limit:
            break
        if extracted is not None:
            raw = extracted.get(art.get("url"))
            if raw is None:
                run.skipped += 1
                continue
        else:
            try:
                raw = ctx.gemini.generate_json(system, (art.get("text") or "")[:12000], schema, 0.0)
                run.cost_usd += EXTRACT_COST_USD
            except GeminiError:
                run.skipped += 1
                continue
        n = ingest(ctx, run, art, raw, limit - added)
        added += n
        run.ok += n
        ctx.con.commit()
    return added


def source_founders(ctx, run, limit: int) -> int:
    return ingest_articles(ctx, run, "founder", fetch_articles(ctx, run, "founder", limit * 3), limit)


def source_creators(ctx, run, limit: int) -> int:
    return ingest_articles(ctx, run, "creator", fetch_articles(ctx, run, "creator", limit * 3), limit)


def source(ctx, run, group: str, limit: int, field: str | None = None) -> int:
    if group == "engineer":
        return source_engineers(ctx, run, limit)
    if group == "founder":
        return source_founders(ctx, run, limit)
    if group == "creator":
        return source_creators(ctx, run, limit)
    if group == "researcher":
        from . import researchers
        return researchers.source(ctx, run, limit, field)
    raise ValueError(group)


__all__ = ["source", "parse_profile", "http"]
