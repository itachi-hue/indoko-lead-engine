"""Email finders. Each returns candidate addresses; policy and verification decide what's usable.

- published_page: addresses the person published on their own pages (personal site, lab page,
  talk page), read through Exa contents. No guessing.
- hunter / prospeo / icypeas: finder APIs. Request shapes are isolated here; confirm each in M0.
- exa_websets: disabled until Indoko's Exa credits are confirmed to include Websets.
"""
from __future__ import annotations

import re
import urllib.parse

import httpx

from .. import db, http, suppression
from ..sources.exa import SEARCH_COST_USD

EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
OBFUSCATED = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\{at\}|\sat\s)\s*"
    r"([A-Za-z0-9\-]+(?:(?:\s*(?:\[dot\]|\(dot\)|\{dot\})\s*|\sdot\s|\.)[A-Za-z0-9\-]+)+)", re.I)
NOISE = ("example.com", "sentry", "@2x", ".png", ".jpg", ".svg", "@types", "@babel", "your-email",
         "email@", "noreply", "no-reply", "wixpress", "domain.com")


def emails_in(text: str) -> list[str]:
    found = list(EMAIL_IN_TEXT.findall(text or ""))
    for local, dom in OBFUSCATED.findall(text or ""):
        dom = re.sub(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\sdot\s)\s*", ".", dom, flags=re.I).replace(" ", "")
        found.append(f"{local}@{dom}")
    out = []
    for e in found:
        n = suppression.normalize_email(e)
        if n and not any(x in n for x in NOISE) and n not in out:
            out.append(n)
    return out


def first_name_at(email: str, display_name: str, domain: str | None) -> bool:
    """first@company: normal for founders, but only trusted on the person's own company domain."""
    parts = [p.lower() for p in re.findall(r"[A-Za-z]+", display_name or "")]
    if not parts or not domain:
        return False
    return email.lower() == f"{parts[0]}@{domain.lower()}"


def name_matches(email: str, display_name: str) -> bool:
    local = email.split("@", 1)[0].lower()
    parts = [p.lower() for p in re.findall(r"[A-Za-z]+", display_name or "") if len(p) > 1]
    if len(parts) < 2:
        return False
    first, last = parts[0], parts[-1]
    # The surname must be there: a shared first name matched colleagues on a staff list (live test, Oct 2026)
    clean = re.sub(r"[^a-z]", "", local)
    return last in clean or clean in (first[0] + last, first + last[0], first + last)


class Finder:
    name = "base"
    cost_usd = 0.0

    def find(self, ctx, person, run) -> list[dict]:
        raise NotImplementedError


class PublishedPageFinder(Finder):
    name = "published_page"

    def find(self, ctx, person, run) -> list[dict]:
        pid = person["person_id"]
        urls = [r["value"] for r in ctx.con.execute(
            "SELECT value FROM signals WHERE person_id=? AND signal_type='personal_site'", (pid,))]
        own_site = set(urls)
        if person["segment"] == "researcher":  # lab or faculty page
            res = ctx.exa.search(f'"{person["display_name"]}" {person["primary_employer"] or ""} faculty page',
                                 num_results=3, category="personal site")
            run.cost_usd += SEARCH_COST_USD
            urls += [r["url"] for r in res if r.get("url")]
        urls = list(dict.fromkeys(u for u in urls if u))[:4]
        if not urls:
            return []
        from .domains import page_text
        pages = []
        for u in urls:   # fetched directly: free (previously Exa contents)
            _, txt = page_text(ctx.client, u)
            if txt:
                pages.append({"url": u, "text": txt})
        out = []
        for page in pages:
            text = page.get("text") or ""
            if person["display_name"] and person["display_name"].split()[-1].lower() not in text.lower():
                continue  # the page must be about this person
            if person["segment"] == "engineer" and person["primary_employer"] and \
                    person["primary_employer"].lower() not in text.lower():
                continue  # common names: only trust a page that also names their employer
            for e in emails_in(text):
                if suppression.is_role_address(e):
                    continue
                if page.get("url") in own_site or name_matches(e, person["display_name"]):
                    out.append({"email": e, "source": self.name, "url": page.get("url")})
        return out


class FounderDomainFinder(Finder):
    """Founders: find the company's domain (free), read its own pages for a published address,
    otherwise list the usual address patterns as guesses that a verifier must confirm."""
    name = "pattern_guess"

    def find(self, ctx, person, run) -> list[dict]:
        from . import domains

        from . import policy

        if person["segment"] == "engineer":   # work address at the employer's known domain
            dom = person["employer_domain"]
            if not policy.ENGINEER_WORK_OK or not dom:
                return []
            return [{"email": e, "source": self.name, "url": f"https://{dom}"}
                    for e in domains.pattern_candidates(person["display_name"], dom)]
        if person["segment"] != "founder" or not person["primary_employer"]:
            return []
        dom = person["employer_domain"]
        if not dom:
            ev = [r["evidence_text"] for r in ctx.con.execute(
                "SELECT evidence_text FROM signals WHERE person_id=? AND evidence_text IS NOT NULL",
                (person["person_id"],))]
            for r in ctx.con.execute(   # the headline of the article they were found in says what the company does
                    "SELECT sr.payload FROM source_records sr JOIN signals s ON s.url=sr.source_native_id "
                    "WHERE s.person_id=? AND sr.source='exa_news'", (person["person_id"],)):
                import json as _json
                payload = _json.loads(r["payload"]) or {}
                ev += [payload.get("title") or "", payload.get("excerpt") or ""]
            dom = domains.company_domain(ctx.client, person["primary_employer"], domains.keywords_from(ev))
            if not dom:
                return []
            db.touch_person(ctx.con, person["person_id"], employer_domain=dom)
        out = []
        for path in domains.EXTRA_PAGES:
            _, text = domains.page_text(ctx.client, f"https://{dom}{path}")
            for e in emails_in(text):
                if e.endswith("@" + dom) and (name_matches(e, person["display_name"])
                                              or first_name_at(e, person["display_name"], dom)):
                    out.append({"email": e, "source": "published_page", "url": f"https://{dom}{path}"})
        if out:
            return out
        return [{"email": e, "source": self.name, "url": f"https://{dom}"}
                for e in domains.pattern_candidates(person["display_name"], dom)]


class HunterFinder(Finder):
    name, cost_usd = "hunter", 0.0  # free tier

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def find(self, ctx, person, run) -> list[dict]:
        parts = (person["display_name"] or "").split()
        if len(parts) < 2:
            return []
        q = {"first_name": parts[0], "last_name": parts[-1], "api_key": self.key}
        if person["employer_domain"]:
            q["domain"] = person["employer_domain"]
        elif person["primary_employer"]:
            q["company"] = person["primary_employer"]
        else:
            return []
        data = http.get_json(self.client, "https://api.hunter.io/v2/email-finder?" + urllib.parse.urlencode(q))
        e = ((data.get("data") or {}).get("email") or "")
        return [{"email": e, "source": self.name}] if e else []


class ProspeoFinder(Finder):
    name, cost_usd = "prospeo", 0.0  # free tier

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def find(self, ctx, person, run) -> list[dict]:
        parts = (person["display_name"] or "").split()
        if len(parts) < 2 or not (person["employer_domain"] or person["primary_employer"]):
            return []
        body = {"first_name": parts[0], "last_name": parts[-1],
                "company": person["employer_domain"] or person["primary_employer"]}
        data = http.post_json(self.client, "https://api.prospeo.io/email-finder", body,
                              headers={"X-KEY": self.key})
        e = ((data.get("response") or {}).get("email") or "")
        return [{"email": e, "source": self.name}] if e else []


class IcypeasFinder(Finder):
    name, cost_usd = "icypeas", 0.02

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def find(self, ctx, person, run) -> list[dict]:
        parts = (person["display_name"] or "").split()
        if len(parts) < 2 or not (person["employer_domain"] or person["primary_employer"]):
            return []
        body = {"firstname": parts[0], "lastname": parts[-1],
                "domainOrCompany": person["employer_domain"] or person["primary_employer"]}
        data = http.post_json(self.client, "https://app.icypeas.com/api/sync/email-search", body,
                              headers={"Authorization": self.key})
        emails = (data.get("item") or data).get("emails") or []
        return [{"email": e.get("email"), "source": self.name} for e in emails if e.get("email")][:1]


GROUP_ORDER = {
    "engineer": ["published_page", "exa_websets", "pattern_guess"],   # guesses only with work_address_ok
    "founder": ["published_page", "pattern_guess", "exa_websets", "hunter", "prospeo", "icypeas"],
    "researcher": ["published_page", "hunter", "prospeo"],
    "creator": ["published_page"],
}


def available(ctx) -> dict[str, Finder]:
    s = ctx.settings
    enabled = set(s.get("emails.providers", []) or [])
    out: dict[str, Finder] = {}
    if "published_page" in enabled:
        out["published_page"] = PublishedPageFinder()
    if "pattern_guess" in enabled:
        out["pattern_guess"] = FounderDomainFinder()
    for name, cls, key in (("hunter", HunterFinder, "HUNTER_API_KEY"), ("prospeo", ProspeoFinder, "PROSPEO_API_KEY"),
                           ("icypeas", IcypeasFinder, "ICYPEAS_API_KEY")):
        if name in enabled and s.secret(key):
            out[name] = cls(ctx.client, s.secret(key))
    return out


def request_hash(provider: str, person_id: str) -> str:
    return db.stable_id("find", provider, person_id)
