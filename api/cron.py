"""Vercel cron entry point: GET /api/cron?job=run-daily

Vercel sends `Authorization: Bearer $CRON_SECRET` on cron invocations; anything else gets 401.
Only the short scheduled jobs are allowed here; sourcing and drafting run by hand.
"""
from __future__ import annotations

import hmac
import io
import os
import sys
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ALLOWED_JOBS = {"run-daily", "report", "retention", "events"}


def authorized(header: str | None, secret: str | None) -> bool:
    if not secret:
        return False
    return hmac.compare_digest(header or "", f"Bearer {secret}")


def run_job(job: str) -> tuple[int, str]:
    from lead_engine.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main([job])
    return code, buf.getvalue()[-4000:]


class handler(BaseHTTPRequestHandler):  # Vercel's Python runtime looks for `handler`
    def do_GET(self):
        if not authorized(self.headers.get("Authorization"), os.environ.get("CRON_SECRET")):
            self.send_response(401)
            self.end_headers()
            return
        job = (parse_qs(urlparse(self.path).query).get("job") or ["run-daily"])[0]
        if job not in ALLOWED_JOBS:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"job not allowed")
            return
        code, out = run_job(job)
        self.send_response(200 if code == 0 else 500)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(out.encode())
