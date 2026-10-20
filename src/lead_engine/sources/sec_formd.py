"""Founders from SEC Form D filings (free; US-only by definition).

Every US company raising private money files a Form D listing its executive officers, the amount
sold and the date. Source: the EDGAR daily form index, then each filing's primary_doc.xml.
SEC fair-access rules: identify yourself in the User-Agent (a contact email) and stay under
10 requests per second.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import xml.etree.ElementTree as ET

from .. import db, http
from ..identity import resolve_person

log = logging.getLogger("lead_engine.formd")
ARCHIVES = "https://www.sec.gov/Archives/edgar"

# Operating companies we want; funds, real estate and banking are excluded.
INDUSTRIES = {
    "Biotechnology", "Pharmaceuticals", "Other Health Care", "Health Insurance", "Hospitals and Physicians",
    "Computers", "Telecommunications", "Other Technology", "Other Energy", "Renewable Energy",
    "Electric Utilities", "Environmental Services", "Manufacturing", "Business Services", "Retailing",
    "Restaurants", "Travel", "Agriculture", "Airlines and Airports", "Energy Conservation",
}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
    "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
MIN_AMOUNT_SOLD = 1_000_000


class Throttle:
    def __init__(self, per_second: float = 8.0):
        self.gap, self.last = 1.0 / per_second, 0.0

    def wait(self):
        delta = time.monotonic() - self.last
        if delta < self.gap:
            time.sleep(self.gap - delta)
        self.last = time.monotonic()


def _headers(ctx) -> dict:
    contact = ctx.settings.secret("SEC_CONTACT_EMAIL") or ctx.settings.get("sourcing.sec_contact_email", "")
    if not contact:
        raise RuntimeError("SEC_CONTACT_EMAIL is not set; the SEC requires a contact email in the User-Agent")
    return {"User-Agent": f"Indoko lead engine {contact}", "Accept-Encoding": "gzip, deflate"}


def daily_index_url(day: dt.date) -> str:
    q = (day.month - 1) // 3 + 1
    return f"{ARCHIVES}/daily-index/{day.year}/QTR{q}/form.{day.strftime('%Y%m%d')}.idx"


def parse_daily_index(text: str) -> list[dict]:
    """Rows of form type 'D' (new filings only, not D/A amendments)."""
    out = []
    for line in text.splitlines():
        if not line.startswith("D "):
            continue
        m = re.match(r"^D\s+(.+?)\s{2,}(\d+)\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(\S+)\s*$", line)
        if m:
            out.append({"company": m.group(1).strip(), "cik": m.group(2), "filed": m.group(3), "path": m.group(4)})
    return out


def primary_doc_url(row: dict) -> str:
    accession = row["path"].rsplit("/", 1)[-1].removesuffix(".txt").replace("-", "")
    return f"{ARCHIVES}/data/{row['cik']}/{accession}/primary_doc.xml"


def _t(node, path: str) -> str:
    el = node.find(path) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_form_d(xml_text: str) -> dict | None:
    root = ET.fromstring(xml_text)
    for el in root.iter():  # drop namespaces so paths stay simple
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    issuer = root.find("primaryIssuer")
    offering = root.find("offeringData")
    if issuer is None or offering is None:
        return None
    people = []
    for rp in root.findall("relatedPersonsList/relatedPersonInfo"):
        rels = [r.text.strip() for r in rp.findall("relatedPersonRelationshipList/relationship") if r.text]
        first, last = _t(rp, "relatedPersonName/firstName"), _t(rp, "relatedPersonName/lastName")
        if first and last:
            people.append({"name": f"{first} {last}".strip(), "relationships": rels})
    amount = _t(offering, "offeringSalesAmounts/totalAmountSold")
    return {
        "company": _t(issuer, "entityName"),
        "state": _t(issuer, "issuerAddress/stateOrCountry"),
        "city": _t(issuer, "issuerAddress/city"),
        "industry": _t(offering, "industryGroup/industryGroupType"),
        "amount_sold": float(amount) if amount.replace(".", "", 1).isdigit() else 0.0,
        "first_sale": _t(offering, "typeOfFiling/dateOfFirstSale/value"),
        "is_amendment": _t(offering, "typeOfFiling/newOrAmendment/isAmendment").lower() == "true",
        "people": people,
    }


def money(n: float) -> str:
    return f"${n / 1e6:.1f}M".replace(".0M", "M") if n >= 1e6 else f"${n:,.0f}"


def title_case(name: str) -> str:
    """EDGAR names are often upper case: 'JANE DOE' -> 'Jane Doe' (keeps mixed-case names as filed)."""
    return " ".join(w.capitalize() if w.isupper() else w for w in name.split())


def wanted(rec: dict) -> bool:
    return (not rec["is_amendment"] and rec["state"] in US_STATES and rec["industry"] in INDUSTRIES
            and rec["amount_sold"] >= MIN_AMOUNT_SOLD and any(
                "Executive Officer" in p["relationships"] for p in rec["people"]))


def ingest(ctx, rec: dict, filing_url: str, filed: str, per_company: int = 2) -> int:
    added = 0
    company = title_case(rec["company"])
    fact = f"{company} reported raising {money(rec['amount_sold'])} in a private funding round (SEC Form D)"
    filed_iso = f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}" if len(filed) == 8 else filed
    for p in [p for p in rec["people"] if "Executive Officer" in p["relationships"]][:per_company]:
        name = title_case(p["name"])
        pid = resolve_person(ctx.con, [("founder_key", f"{name.lower()}|{company.lower()}")], name,
                             segment="founder", group_source="cold", primary_employer=company,
                             title="Executive Officer", us_located=1,
                             country=f"{title_case(rec['city'])}, {rec['state']}".strip(", "))
        db.add_signal(ctx.con, pid, "funding", str(rec["amount_sold"]), filing_url, evidence_text=fact,
                      evidence_date=filed_iso, url=filing_url)
        db.upsert_source_record(ctx.con, "sec_formd", filing_url, "filing",
                                {k: rec[k] for k in ("company", "state", "industry", "amount_sold", "first_sale")})
        added += 1
    return added


def source(ctx, run, limit: int, days_back: int = 10, today: dt.date | None = None) -> int:
    headers = _headers(ctx)
    throttle = Throttle()
    today = today or dt.date.today()
    added = 0
    for back in range(1, days_back + 1):
        if added >= limit:
            break
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue  # no EDGAR filings on weekends
        throttle.wait()
        try:
            index = http.request(ctx.client, "GET", daily_index_url(day), headers=headers).text
        except http.HTTPError as exc:
            if exc.status == 404:
                continue  # holiday or not yet published
            raise
        for row in parse_daily_index(index):
            if added >= limit:
                break
            if db.get_cursor(ctx.con, "sec_formd_seen", row["path"]):
                continue
            url = primary_doc_url(row)
            throttle.wait()
            try:
                rec = parse_form_d(http.request(ctx.client, "GET", url, headers=headers).text)
            except (http.HTTPError, ET.ParseError):
                run.skipped += 1
                continue
            db.set_cursor(ctx.con, "sec_formd_seen", row["path"], "1")
            if not rec or not wanted(rec):
                run.skipped += 1
                continue
            n = ingest(ctx, rec, url, row["filed"])
            added += n
            run.ok += n
            ctx.con.commit()
    ctx.con.commit()
    return added
