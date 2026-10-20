"""Instantly API v2 client.

Endpoint paths and payload shapes are kept in this one module. Several details are listed in
docs/DESIGN.md section 16 as "verify in M0" (plan access, custom-variable format, the
skip-if-in-workspace flag, where reply interest status lives); if the live API differs, only this
file changes.
"""
from __future__ import annotations

import httpx

from .. import http

BASE = "https://api.instantly.ai/api/v2"


class InstantlyError(Exception):
    pass


class InstantlyClient:
    def __init__(self, client: httpx.Client, api_key: str):
        self.client = client
        self.api_key = api_key

    def _h(self) -> dict:
        if not self.api_key:
            raise InstantlyError("INSTANTLY_API_KEY is not set")
        return {"Authorization": f"Bearer {self.api_key}"}

    # ---- access ----
    def ping(self) -> dict:
        """True when the key works and the plan includes API access."""
        try:
            data = http.get_json(self.client, f"{BASE}/campaigns?limit=1", headers=self._h())
            return {"ok": True, "campaigns_visible": len(data.get("items", []))}
        except http.HTTPError as e:
            return {"ok": False, "status": e.status, "detail": e.body[:200]}

    # ---- campaigns ----
    def list_campaigns(self) -> list[dict]:
        out, cursor = [], None
        while True:
            url = f"{BASE}/campaigns?limit=100" + (f"&starting_after={cursor}" if cursor else "")
            data = http.get_json(self.client, url, headers=self._h())
            out.extend(data.get("items", []))
            cursor = data.get("next_starting_after")
            if not cursor:
                return out

    def pause_campaign(self, campaign_id: str) -> None:
        http.request(self.client, "POST", f"{BASE}/campaigns/{campaign_id}/pause", headers=self._h())

    # ---- leads ----
    def add_lead(self, campaign_id: str, email: str, first_name: str, custom_variables: dict[str, str]) -> dict:
        payload = {
            "campaign": campaign_id,
            "email": email,
            "first_name": first_name,
            "custom_variables": {k: str(v) for k, v in custom_variables.items()},
            "skip_if_in_workspace": True,
        }
        return http.post_json(self.client, f"{BASE}/leads", payload, headers=self._h())

    def find_lead(self, campaign_id: str, email: str) -> dict | None:
        data = http.post_json(self.client, f"{BASE}/leads/list",
                              {"campaign": campaign_id, "search": email, "limit": 10}, headers=self._h())
        for item in data.get("items", []):
            if (item.get("email") or "").lower() == email.lower():
                return item
        return None

    def list_leads(self, campaign_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            body = {"campaign": campaign_id, "limit": 100}
            if cursor:
                body["starting_after"] = cursor
            data = http.post_json(self.client, f"{BASE}/leads/list", body, headers=self._h())
            out.extend(data.get("items", []))
            cursor = data.get("next_starting_after")
            if not cursor:
                return out

    def delete_lead(self, lead_id: str) -> None:
        http.request(self.client, "DELETE", f"{BASE}/leads/{lead_id}", headers=self._h())

    # ---- emails (replies) ----
    def list_received_emails(self, since_iso: str | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            url = f"{BASE}/emails?limit=100&email_type=received"
            if since_iso:
                url += f"&min_timestamp_created={since_iso}"
            if cursor:
                url += f"&starting_after={cursor}"
            data = http.get_json(self.client, url, headers=self._h())
            out.extend(data.get("items", []))
            cursor = data.get("next_starting_after")
            if not cursor:
                return out

    # ---- blocklist ----
    def block(self, value: str) -> None:
        http.post_json(self.client, f"{BASE}/block-lists-entries", {"bl_value": value}, headers=self._h())
