"""Engineers from USPTO patent data via PatentsView (free API key: https://patentsview.org).

Inventors on recent patents assigned to large tech companies, with a US address on the patent.
Every person arrives with dated, linked evidence (their patents), unlike profile search.

Endpoint and field names follow the PatentSearch API (search.patentsview.org); verify them with
the first live call, since this was written before a key was available.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from .. import db, http
from ..identity import resolve_person
from . import mobility
from .cold import COMPANIES, engineer_companies

log = logging.getLogger("lead_engine.patents")
ENDPOINT = "https://search.patentsview.org/api/v1/patent/"
FIELDS = ["patent_id", "patent_title", "patent_date", "inventors.inventor_name_first",
          "inventors.inventor_name_last", "inventors.inventor_city", "inventors.inventor_state",
          "inventors.inventor_country", "assignees.assignee_organization"]
ASSIGNEE_MATCH = {  # company -> text that appears in its assignee names
    "Google": "Google", "Meta": "Meta Platforms", "Apple": "Apple Inc", "Amazon": "Amazon Technologies",
    "Microsoft": "Microsoft Technology Licensing", "NVIDIA": "NVIDIA", "Netflix": "Netflix",
}


def query(ctx, company: str, since: str, size: int = 100) -> list[dict]:
    key = ctx.settings.secret("PATENTSVIEW_API_KEY")
    if not key:
        raise RuntimeError("PATENTSVIEW_API_KEY is not set (free key from patentsview.org)")
    body = {"q": {"_and": [{"_gte": {"patent_date": since}},
                           {"_contains": {"assignees.assignee_organization": ASSIGNEE_MATCH[company]}}]},
            "f": FIELDS, "o": {"size": size}, "s": [{"patent_date": "desc"}]}
    data = http.post_json(ctx.client, ENDPOINT, body, headers={"X-Api-Key": key})
    return data.get("patents") or []


def inventor_history(ctx, first: str, last: str) -> list[dict]:
    """Every patent filed under this inventor name, oldest first: [{date, country, city}]."""
    key = ctx.settings.secret("PATENTSVIEW_API_KEY")
    body = {"q": {"_and": [{"inventors.inventor_name_first": first}, {"inventors.inventor_name_last": last}]},
            "f": ["patent_date", "inventors.inventor_name_first", "inventors.inventor_name_last",
                  "inventors.inventor_city", "inventors.inventor_country"],
            "o": {"size": 100}, "s": [{"patent_date": "asc"}]}
    data = http.post_json(ctx.client, ENDPOINT, body, headers={"X-Api-Key": key})
    out = []
    for pat in data.get("patents") or []:
        for inv in pat.get("inventors") or []:
            if (inv.get("inventor_name_first") or "").strip().lower() == first.lower() and \
                    (inv.get("inventor_name_last") or "").strip().lower() == last.lower():
                out.append({"date": pat.get("patent_date"), "country": inv.get("inventor_country"),
                            "city": inv.get("inventor_city")})
    return out


def inventors_from(patents: list[dict], company: str, min_patents: int = 2) -> list[dict]:
    """Group by inventor name; keep US-addressed inventors with at least `min_patents` patents."""
    by: dict[str, dict] = defaultdict(lambda: {"patents": [], "city": "", "state": "", "country": ""})
    for pat in patents:
        for inv in pat.get("inventors") or []:
            first, last = (inv.get("inventor_name_first") or "").strip(), (inv.get("inventor_name_last") or "").strip()
            if not first or not last:
                continue
            name = f"{first} {last}"
            rec = by[name]
            rec["patents"].append({"id": pat.get("patent_id"), "title": (pat.get("patent_title") or "").strip(),
                                   "date": pat.get("patent_date")})
            rec["city"] = inv.get("inventor_city") or rec["city"]
            rec["state"] = inv.get("inventor_state") or rec["state"]
            rec["country"] = inv.get("inventor_country") or rec["country"]
    out = []
    for name, rec in by.items():
        if rec["country"] == "US" and len(rec["patents"]) >= min_patents:
            out.append({"name": name, "company": company, **rec})
    out.sort(key=lambda r: -len(r["patents"]))
    return out


def ingest(ctx, inv: dict) -> str:
    company = inv["company"]
    pid = resolve_person(ctx.con, [("inventor_key", f"{inv['name'].lower()}|{company.lower()}")], inv["name"],
                         segment="engineer", group_source="cold", primary_employer=company,
                         employer_domain=COMPANIES.get(company), title=f"Inventor at {company}", us_located=1,
                         country=f"{inv['city']}, {inv['state']}".strip(", "))
    for pat in inv["patents"][:4]:
        url = f"https://patents.google.com/patent/US{pat['id']}"
        db.add_signal(ctx.con, pid, "patent", 1, url, evidence_text=f'Patent: "{pat["title"]}"',
                      evidence_date=pat["date"], url=url)
    return pid


def source(ctx, run, limit: int, months_back: int = 18) -> int:
    since = (dt.date.today() - dt.timedelta(days=30 * months_back)).isoformat()
    companies = [c for c in ASSIGNEE_MATCH if c in (engineer_companies(ctx) or ASSIGNEE_MATCH)]
    per_company = max(1, -(-limit // len(companies)))
    added = 0
    for company in companies:
        if added >= limit:
            break
        try:
            patents = query(ctx, company, since, size=1000 if mobility.mode(ctx) == "require" else 100)
        except http.HTTPError as exc:
            run.note(f"PatentsView {company}: HTTP {exc.status}")
            run.failed += 1
            continue
        taken = 0
        for inv in inventors_from(patents, company):
            if added >= limit or taken >= per_company:
                break
            if db.find_person_by_identifier(ctx.con, "inventor_key", f"{inv['name'].lower()}|{company.lower()}"):
                continue
            move = None
            if mobility.mode(ctx) != "off":
                first, last = inv["name"].split(" ", 1)
                try:
                    move = mobility.inventor_move(inventor_history(ctx, first, last), mobility.within_years(ctx))
                except http.HTTPError as exc:
                    run.note(f"PatentsView history {inv['name']}: HTTP {exc.status}")
                if mobility.mode(ctx) == "require" and not move:
                    run.skipped += 1
                    continue
            pid = ingest(ctx, inv)
            if move:
                mobility.record(ctx.con, pid, move, f"https://patents.google.com/?inventor={inv['name']}")
            taken += 1
            added += 1
            run.ok += 1
        ctx.con.commit()
    return added
