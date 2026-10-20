"""Shared fixtures: isolated settings, an in-memory-style database, and a fake HTTP router."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from lead_engine import db
from lead_engine.config import load_settings
from lead_engine.context import Context

CONFIG = """
[sender]
name = "Karishma"
postal_address = "1 Market St, San Francisco, CA 94105"
[sender.booking_links]
engineer = "https://cal.com/indoko/eng"
founder = "https://cal.com/indoko/founder"
researcher = "https://cal.com/indoko/research"
creator = "https://cal.com/indoko/creator"
warm = "https://cal.com/indoko/warm"
[sheet]
spreadsheet_id = "sheet123"
[gemini]
model = "gemini-test"
[instantly.campaigns]
engineer = "camp-eng"
founder = "camp-fnd"
researcher = "camp-res"
creator = "camp-cre"
warm = "camp-warm"
[sending]
inboxes = 4
pilot_start = "2026-11-23"
"""


class Router:
    """Fake HTTP: register (method, url-regex) -> handler(request) or static response."""

    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, pattern: str, response=None, status: int = 200, handler=None):
        self.routes.append((method.upper(), re.compile(pattern), handler or (status, response)))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for method, pat, target in reversed(self.routes):
            if method == request.method and pat.search(str(request.url)):
                if callable(target):
                    return target(request)
                status, body = target
                if isinstance(body, (bytes, str)):
                    return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode())
                return httpx.Response(status, json=body if body is not None else {})
        return httpx.Response(404, json={"error": f"no route for {request.method} {request.url}"})

    def calls_to(self, pattern: str, method: str | None = None) -> list[httpx.Request]:
        rx = re.compile(pattern)
        return [c for c in self.calls if rx.search(str(c.url)) and (method is None or c.method == method)]

    @staticmethod
    def body(request: httpx.Request):
        return json.loads(request.content or b"{}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text("EXA_API_KEY=exa-k\nGEMINI_API_KEY=gem-k\nINSTANTLY_API_KEY=inst-k\n")
    return tmp_path


@pytest.fixture
def settings(root, tmp_path):
    s = load_settings(root, env={})
    s.data["paths"] = {"data_dir": str(tmp_path / "data"), "lock_file": str(tmp_path / "run.lock")}
    return s


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def ctx(settings, router) -> Context:
    from lead_engine.google_api import StaticAuth

    c = Context(settings=settings, transport=httpx.MockTransport(router))
    c.google_auth = StaticAuth()
    return c


@pytest.fixture
def con(ctx):
    return ctx.con


def make_person(con, name="Ada Lovelace", segment="engineer", email="ada@lovelace.dev", **kw):
    fields = dict(segment=segment, group_source=kw.pop("group_source", "cold"), us_located=1,
                  email=email, email_status="verified" if email else None,
                  primary_employer=kw.pop("employer", "Google"), employer_domain=kw.pop("employer_domain", "google.com"))
    fields.update(kw)
    pid = db.create_person(con, name, **fields)
    if email:
        db.add_identifier(con, pid, "email", email)
    con.commit()
    return pid
