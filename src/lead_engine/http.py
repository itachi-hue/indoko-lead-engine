"""Shared HTTP layer: one retry policy for every external API.

Policy (docs/DESIGN.md section 12): 429 and 5xx are retried up to 4 times with exponential
backoff, honoring Retry-After. Other 4xx responses raise immediately so the caller can fail
that one item without failing the whole run.
"""
from __future__ import annotations

import time
from typing import Callable

import httpx

MAX_RETRIES = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HTTPError(Exception):
    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(float(ra), 120.0)
        except ValueError:
            pass
    return min(2 ** attempt, 60)


def make_client(timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
                headers: dict | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=True,
                        headers={"User-Agent": "indoko-lead-engine/0.1", **(headers or {})})


def request(client: httpx.Client, method: str, url: str, *, sleep: Callable[[float], None] = time.sleep,
            retries: int = MAX_RETRIES, **kwargs) -> httpx.Response:
    """Send a request with the shared retry policy. Returns the response on 2xx/3xx."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == retries:
                break
            sleep(min(2 ** attempt, 60))
            continue
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            sleep(_retry_after(resp, attempt))
            continue
        if resp.status_code >= 400:
            raise HTTPError(resp.status_code, resp.reason_phrase or "error", resp.text[:500])
        return resp
    raise HTTPError(0, f"transport failure after {retries} retries: {last_exc}")


def get_json(client: httpx.Client, url: str, **kwargs):
    return request(client, "GET", url, **kwargs).json()


def post_json(client: httpx.Client, url: str, payload, **kwargs):
    return request(client, "POST", url, json=payload, **kwargs).json()
