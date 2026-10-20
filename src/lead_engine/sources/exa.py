"""Exa API client: search (people, companies, news), page contents, and Websets enrichment."""
from __future__ import annotations

import httpx

from .. import http

BASE = "https://api.exa.ai"
SEARCH_COST_USD = 0.007        # per search request, up to 10 results (exa.ai/pricing, Oct 2026)
CONTENTS_COST_USD = 0.001      # per page, per content type


class ExaError(Exception):
    pass


class ExaClient:
    def __init__(self, client: httpx.Client, api_key: str):
        self.client, self.api_key = client, api_key

    def _h(self) -> dict:
        if not self.api_key:
            raise ExaError("EXA_API_KEY is not set")
        return {"x-api-key": self.api_key}

    def ping(self) -> dict:
        try:
            self.search("staff software engineer", num_results=1)
            return {"ok": True}
        except http.HTTPError as e:
            return {"ok": False, "status": e.status, "detail": e.body[:200]}

    def search(self, query: str, num_results: int = 10, category: str | None = None,
               include_text: bool = False, start_published_date: str | None = None) -> list[dict]:
        body: dict = {"query": query, "numResults": num_results, "type": "auto"}
        if category:
            body["category"] = category
        if start_published_date:
            body["startPublishedDate"] = start_published_date
        if include_text:
            body["contents"] = {"text": {"maxCharacters": 4000}}
        data = http.post_json(self.client, f"{BASE}/search", body, headers=self._h())
        return data.get("results", [])

    def contents(self, urls: list[str], max_chars: int = 20000) -> list[dict]:
        if not urls:
            return []
        data = http.post_json(self.client, f"{BASE}/contents",
                              {"urls": urls, "text": {"maxCharacters": max_chars}}, headers=self._h())
        return data.get("results", [])
