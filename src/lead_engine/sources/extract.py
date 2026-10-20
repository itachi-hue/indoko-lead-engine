"""Structured extraction from an article, with every extracted value verified against the text.

Gemini reads a funding or press article and returns people and facts as JSON. Anything Gemini
returns that does not literally appear in the article (names, companies, amounts, outlets) is
dropped, so evidence can never contain an invented fact.
"""
from __future__ import annotations

import re

from ..drafting.checks import numbers_in

FOUNDER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "company": {"type": "STRING"},
        "round": {"type": "STRING"},
        "amount": {"type": "STRING"},
        "location": {"type": "STRING"},
        "founders": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "title": {"type": "STRING"}}, "required": ["name"]}},
    },
    "required": ["company", "founders"],
}

CREATOR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "people": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "discipline": {"type": "STRING"},
            "achievement": {"type": "STRING", "description": "award, festival selection, exhibition or feature, "
                                                             "copied exactly from the article"},
            "location": {"type": "STRING"}}, "required": ["name", "achievement"]}},
    },
    "required": ["people"],
}

FOUNDER_SYSTEM = ("Extract the startup funding facts from the article. Copy names, the company, the round "
                  "(e.g. Seed, Series A), the amount and the company's location exactly as written. Only "
                  "include founders the article names. If something is not in the article, leave it empty.")
CREATOR_SYSTEM = ("Extract the artists, filmmakers, musicians, writers, designers or media people this "
                  "article is about. For each, copy their name and one concrete achievement (an award, "
                  "festival selection, exhibition, or feature) exactly as written, plus their location if "
                  "stated. Skip anyone without a concrete achievement in the article.")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def appears(value: str, text: str) -> bool:
    v = _norm(value)
    return bool(v) and v in _norm(text)


def amount_appears(amount: str, text: str) -> bool:
    nums = numbers_in(amount or "")
    return bool(nums) and nums <= numbers_in(text)


def verify_founder_record(rec: dict, text: str) -> dict | None:
    """Keep only what the article supports. Returns None if the company or no founder survives."""
    company = (rec.get("company") or "").strip()
    if not appears(company, text):
        return None
    founders = [f for f in rec.get("founders") or [] if appears(f.get("name", ""), text)
                and len(f.get("name", "").split()) >= 2]
    if not founders:
        return None
    out = {"company": company, "founders": founders}
    if appears(rec.get("round", ""), text):
        out["round"] = rec["round"].strip()
    if amount_appears(rec.get("amount", ""), text):
        out["amount"] = rec["amount"].strip()
    if appears(rec.get("location", ""), text):
        out["location"] = rec["location"].strip()
    return out


def verify_creator_records(data: dict, text: str) -> list[dict]:
    out = []
    for p in data.get("people") or []:
        name, ach = p.get("name", ""), p.get("achievement", "")
        if len(name.split()) < 2 or not appears(name, text) or not appears(ach, text):
            continue
        rec = {"name": name.strip(), "achievement": ach.strip()}
        if appears(p.get("discipline", ""), text):
            rec["discipline"] = p["discipline"].strip()
        if appears(p.get("location", ""), text):
            rec["location"] = p["location"].strip()
        out.append(rec)
    return out
