"""Builds the objects a command needs (settings, database, HTTP client, API clients).

Everything external is created lazily so commands that don't need a service never touch it,
and tests can inject fakes by setting attributes directly.
"""
from __future__ import annotations

import json
import logging
from functools import cached_property
from pathlib import Path

import httpx

from . import http
from .config import Settings, load_settings


class Context:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None,
                 dry_run: bool = False):
        self.settings = settings or load_settings()
        from .emails import policy
        policy.configure(self.settings)
        self.transport = transport
        self.dry_run = dry_run

    @cached_property
    def con(self):
        from . import storage

        con = storage.connect(self.settings)
        storage.init(con)
        return con

    @cached_property
    def client(self) -> httpx.Client:
        return http.make_client(transport=self.transport)

    @cached_property
    def google_auth(self):
        from .google_api import ServiceAccountAuth

        raw = self.settings.secret("GOOGLE_SERVICE_ACCOUNT_JSON")
        if raw:                                   # hosted: key JSON in an env var
            return ServiceAccountAuth.from_info(json.loads(raw), self.client)
        key = self.settings.secret("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not key:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_FILE (or GOOGLE_SERVICE_ACCOUNT_JSON) is not set")
        return ServiceAccountAuth(key, self.client)

    @cached_property
    def sheets(self):
        from .google_api import Sheets

        sid = self.settings.get("sheet.spreadsheet_id")
        if not sid:
            raise RuntimeError("sheet.spreadsheet_id is not set in config.toml")
        return Sheets(self.client, self.google_auth, sid)

    @cached_property
    def drive(self):
        from .google_api import Drive

        return Drive(self.client, self.google_auth)

    @cached_property
    def instantly(self):
        from .instantly.client import InstantlyClient

        return InstantlyClient(self.client, self.settings.secret("INSTANTLY_API_KEY"))

    @cached_property
    def exa(self):
        from .sources.exa import ExaClient

        return ExaClient(self.client, self.settings.secret("EXA_API_KEY"))

    @cached_property
    def gemini(self):
        from .drafting.gemini import GeminiClient

        return GeminiClient(self.client, self.settings.secret("GEMINI_API_KEY"),
                            self.settings.get("gemini.model"))


def setup_logging(data_dir: Path, verbose: bool = False) -> None:
    """Logs carry IDs and counts only, never email addresses or bodies."""
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("lead_engine")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(logs / "lead_engine.log")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)
