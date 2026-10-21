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
    # US-focused: generic "festival award" searches returned only UK/Iran/Greece/Philippines winners (live, Oct 2026)
    "creator": ["Sundance Film Festival award winners announced", "Tribeca Festival award winners",
                "SXSW film festival jury award winners", "Grammy nominees announced first-time nominee",
                "Guggenheim Fellowship artists filmmakers awarded", "New York gallery solo exhibition emerging artist",
                "Los Angeles artist wins award", "US documentary filmmaker wins award"],
}
SENIOR_TITLE = re.compile(r"\b(staff|principal|distinguished|senior staff|fellow|director|"
                          r"head of|vp|lead)\b", re.I)


def _queries(ctx, group: str) -> list[str]:
    custom = ctx.settings.get(f"sourcing.queries.{group}", [])
    if custom:
        return list(custom)
    only = engineer_companies(ctx) if group == "engineer" else None
    return [q for q in DEFAULT_QUERIES[group] if not only or any(f" at {c} " in q for c in only)]


def engineer_companies(ctx) -> list[str]:
    """sourcing.engineer_companies: which employers to source engineers from (empty = all defaults).
    Prefer companies whose mail servers confirm mailboxes, so guessed work addresses can be verified."""
    return list(ctx.settings.get("sourcing.engineer_companies", []) or [])


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
def _from_entity(result: dict) -> dict | None:
    """Exa people results carry a structured person entity (verified live, Oct 2026)."""
    for ent in result.get("entities") or []:
        if ent.get("type") != "person":
            continue
        props = ent.get("properties") or {}
        name = (props.get("name") or "").strip()
        current = [j for j in props.get("workHistory") or [] if not (j.get("dates") or {}).get("to")]
        if not name or not current:
            return None
        job = current[0]
        company_name = ((job.get("company") or {}).get("name") or "").strip()
        company = next((c for c in COMPANIES if c.lower() == company_name.lower()), None)
        return {"name": name, "headline": f"{job.get('title') or ''} at {company_name}".strip(),
                "company": company, "url": result.get("url", ""),
                "location_hint": props.get("location") or job.get("location") or ""}
    return None


def parse_profile(result: dict) -> dict | None:
    """Exa person entity if present; otherwise a 'Name - Headline - Company | LinkedIn' title."""
    ent = _from_entity(result)
    if ent:
        return ent
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


NOT_EVIDENCE_DOMAINS = (
    "linkedin.com", "contactout.com", "rocketreach.co", "zoominfo.com", "signalhire.com", "apollo.io",
    "theorg.com", "facebook.com", "x.com", "twitter.com", "instagram.com", "github.com", "scholar.google.",
    "crunchbase.com", "peoplefinders", "spokeo", "whitepages", "luma.com", "lu.ma", "baiten.cn",
    "uspto.report", "patents.us", "trea.com", "getprog.ai", "rocketreach", "lusha.com", "clay.com",
)
PROFILE_TITLE = re.compile(r"\b(engineer|developer|manager|director|scientist|founder)\b.{0,40}\bat\b", re.I)
EVIDENCE_DOMAINS = {
    "patent": ("patents.google.com", "uspto.gov", "patents.justia.com", "freepatentsonline.com",
               "patentimages.storage.googleapis.com"),
    "paper": ("arxiv.org", "doi.org", "acm.org", "ieee.org", "openreview.net", "aclanthology.org",
              "neurips.cc", "mlr.press", "usenix.org", "semanticscholar.org"),
}


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "")).strip()


def evidence_ok(kind: str, name: str, company: str | None, result: dict) -> bool:
    """Is this search result really about this person, and real evidence of `kind`?

    Live test (Oct 2026): name-only matching attached other people's patents (same name, different
    field) and treated LinkedIn/data-broker pages as patents. So: the employer must be mentioned,
    the source must suit the kind, and the title must say more than the name.
    """
    url, dom = result.get("url") or "", _domain(result.get("url"))
    if any(b in dom for b in NOT_EVIDENCE_DOMAINS):
        return False
    if kind in EVIDENCE_DOMAINS and not any(dom.endswith(d) or d in dom for d in EVIDENCE_DOMAINS[kind]):
        return False
    title = _clean_title(result.get("title"))
    rest = re.sub(re.escape(name), "", title, flags=re.I)
    if len(re.findall(r"[A-Za-z]{3,}", rest)) < 3:
        return False
    if re.search(re.escape(name), title, re.I) and PROFILE_TITLE.search(rest):
        return False  # "Name - Staff Engineer at X" is a profile page, not a talk or patent
    text = f"{title} {(result.get('text') or '')[:4000]}"
    if not _names_person(name, text):
        return False
    return bool(company) and company.lower() in text.lower()


def _add_evidence(ctx, run, pid: str, name: str, company: str | None, kind: str, query: str) -> int:
    added = 0
    for r in _search(ctx, run, query, num_results=5, include_text=True):
        if not evidence_ok(kind, name, company, r):
            continue
        title = _clean_title(r.get("title"))[:160]
        date = (r.get("publishedDate") or "")[:10] or None
        label = {"patent": "Patent", "talk": "Talk", "paper": "Paper", "project": "Open-source project"}[kind]
        db.add_signal(ctx.con, pid, kind, 1, r.get("url"), evidence_text=f'{label}: "{title}"',
                      evidence_date=date, url=r.get("url"))
        added += 1
        if added >= 2:
            break
    return added


WORK_WORDS = re.compile(r"\b(engineer|engineering|software|developer|research|scientist|founder|startup|"
                        r"machine learning|ml|ai|systems|product|code|programming)\b", re.I)


def _add_personal_site(ctx, run, pid: str, name: str, company: str | None = None) -> None:
    parts = [p.lower() for p in re.findall(r"[A-Za-z]{2,}", name)]
    for r in _search(ctx, run, f"{name} personal website", num_results=3, category="personal site",
                     include_text=True):
        url, dom = r.get("url") or "", _domain(r.get("url"))
        if any(b in dom for b in NOT_EVIDENCE_DOMAINS):
            continue
        if not parts or parts[-1] not in dom.replace("-", "").replace(".", ""):
            continue  # the address should carry their surname
        text = f"{r.get('title', '')} {(r.get('text') or '')[:3000]}"
        if not _names_person(name, text):
            continue
        if company and company.lower() not in text.lower() and not WORK_WORDS.search(text):
            continue  # likely someone else with the same name
        db.add_signal(ctx.con, pid, "personal_site", url, url)
        return


def source_engineers(ctx, run, limit: int) -> int:
    category = ctx.settings.get("sourcing.people_category", "people")
    queries = _queries(ctx, "engineer")
    per_query = max(1, -(-limit // len(queries)))  # spread people across companies and levels
    added = 0
    for q in queries:
        if added >= limit:
            break
        from_this_query = 0
        for r in _search(ctx, run, q, num_results=10, category=category, include_text=True):
            if added >= limit or from_this_query >= per_query:
                break
            prof = parse_profile(r)
            if not prof or not prof["company"] or not SENIOR_TITLE.search(prof["headline"]):
                run.skipped += 1
                continue
            surname = prof["name"].split()[-1]
            if len(surname.strip(".")) < 2 or surname.endswith("."):
                run.skipped += 1          # "Daniel E." is too ambiguous to match evidence to
                continue
            if db.find_person_by_identifier(ctx.con, "linkedin", prof["url"]):
                continue                  # already sourced; don't spend on evidence again
            from_this_query += 1
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
            _add_personal_site(ctx, run, pid, prof["name"], prof["company"])
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
    queries = _queries(ctx, group)
    per_query = max(1, -(-max_articles // len(queries)))   # spread across searches: one query alone
    out, seen = [], set()                                    # returned 10 copies of the same story
    for q in queries:
        if len(out) >= max_articles:
            break
        n = max(per_query, 3)
        taken = 0
        for art in _search(ctx, run, q, num_results=n, category="news", include_text=True,
                           start_published_date=_since(days)):
            url = art.get("url")
            if taken >= per_query or not url or url in seen or not art.get("text"):
                continue
            seen.add(url)
            taken += 1
            out.append({k: art.get(k) for k in ("url", "title", "publishedDate", "text")})
    return out[:max_articles]


def _ingest_founder(ctx, run, art: dict, raw: dict, room: int) -> int:
    text = art.get("text") or ""
    rec = extract.verify_founder_record(raw or {}, text)
    if not rec:
        run.skipped += 1
        return 0
    # Only a location the article states counts; guessing from the text (e.g. a photo caption) is not enough
    loc = us_located(rec.get("location")) if rec.get("location") else None
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
                                {"title": art.get("title"), "url": art.get("url"), "record": rec,
                                 "excerpt": text[:4000]})   # used later to confirm the company's website
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


FREE_DEFAULTS = {"engineer": "patents", "founder": "sec_form_d"}


def source(ctx, run, group: str, limit: int, field: str | None = None, via: str | None = None) -> int:
    """Free sources by default (cost); `via="exa"` uses the paid Exa search instead."""
    via = via or ctx.settings.get(f"sourcing.{group}_source", FREE_DEFAULTS.get(group, "exa"))
    if group == "engineer":
        if via == "patents":
            from . import patents
            return patents.source(ctx, run, limit)
        return source_engineers(ctx, run, limit)
    if group == "founder":
        if via == "sec_form_d":
            from . import sec_formd
            return sec_formd.source(ctx, run, limit)
        return source_founders(ctx, run, limit)
    if group == "creator":
        return source_creators(ctx, run, limit)
    if group == "researcher":
        from . import researchers
        return researchers.source(ctx, run, limit, field)
    raise ValueError(group)


__all__ = ["source", "parse_profile", "http"]
