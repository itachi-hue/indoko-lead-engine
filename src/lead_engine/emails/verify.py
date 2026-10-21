"""Email verification behind one interface (docs/DESIGN.md section 6).

Results are normalized to: deliverable | catch_all | unknown | undeliverable.
Provider request shapes are isolated here; confirm the chosen provider's API in M0.
A self-hosted verifier (Reacher) needs outbound port 25, which home connections block.
"""
from __future__ import annotations

import urllib.parse

import httpx

from .. import http


class Verifier:
    name = "none"
    cost_usd = 0.0

    def verify(self, email: str) -> str:
        raise NotImplementedError


class MillionVerifier(Verifier):
    name, cost_usd = "millionverifier", 0.001

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def verify(self, email: str) -> str:
        url = f"https://api.millionverifier.com/api/v3/?api={self.key}&email={urllib.parse.quote(email)}&timeout=20"
        res = (http.get_json(self.client, url).get("result") or "").lower()
        return {"ok": "deliverable", "catch_all": "catch_all", "invalid": "undeliverable",
                "disposable": "undeliverable"}.get(res, "unknown")


class ZeroBounce(Verifier):
    name, cost_usd = "zerobounce", 0.008

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def verify(self, email: str) -> str:
        url = f"https://api.zerobounce.net/v2/validate?api_key={self.key}&email={urllib.parse.quote(email)}"
        res = (http.get_json(self.client, url).get("status") or "").lower()
        return {"valid": "deliverable", "catch-all": "catch_all", "invalid": "undeliverable",
                "spamtrap": "undeliverable", "abuse": "undeliverable", "do_not_mail": "undeliverable"
                }.get(res, "unknown")


class Reoon(Verifier):
    """Reoon power mode (SMTP mailbox check + catch-all detection). Free plan: ~600 checks a month."""
    name, cost_usd = "reoon", 0.0

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def verify(self, email: str) -> str:
        url = (f"https://emailverifier.reoon.com/api/v1/verify?email={urllib.parse.quote(email)}"
               f"&key={self.key}&mode=power")
        data = http.get_json(self.client, url)
        if data.get("is_catch_all"):
            return "catch_all"
        res = (data.get("status") or "").lower()
        return {"safe": "deliverable", "catch_all": "catch_all", "invalid": "undeliverable",
                "disabled": "undeliverable", "disposable": "undeliverable", "spamtrap": "undeliverable"
                }.get(res, "unknown")


class Reacher(Verifier):
    name, cost_usd = "reacher", 0.0

    def __init__(self, client: httpx.Client, base_url: str):
        self.client, self.base = client, base_url.rstrip("/")

    def verify(self, email: str) -> str:
        res = (http.post_json(self.client, f"{self.base}/v0/check_email", {"to_email": email})
               .get("is_reachable") or "").lower()
        return {"safe": "deliverable", "risky": "catch_all", "invalid": "undeliverable"}.get(res, "unknown")


def build(ctx) -> Verifier | None:
    name = (ctx.settings.get("emails.verifier") or "").lower()
    key = ctx.settings.secret("VERIFIER_API_KEY")
    if name == "millionverifier" and key:
        return MillionVerifier(ctx.client, key)
    if name == "zerobounce" and key:
        return ZeroBounce(ctx.client, key)
    if name == "reoon" and key:
        return Reoon(ctx.client, key)
    if name.startswith("reacher"):
        return Reacher(ctx.client, ctx.settings.get("emails.reacher_url", "http://localhost:8080"))
    return None
