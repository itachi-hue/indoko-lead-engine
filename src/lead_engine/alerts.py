"""Alerts to Vivek through an internal inbox, never through the sending domains.

ALERT_SMTP_URL format: smtp+ssl://user:password@host:465/to@example.com (or smtp:// for STARTTLS).
Without it, alerts go to the log and data/alerts.log only.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import urllib.parse
from email.message import EmailMessage

from . import db

log = logging.getLogger("lead_engine.alerts")


def send(ctx, subject: str, body: str) -> bool:
    line = f"{db.now()} {subject}: {body}"
    log.error("ALERT %s", line)
    path = ctx.settings.data_dir / "alerts.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    url = ctx.settings.secret("ALERT_SMTP_URL")
    if not url or ctx.dry_run:
        return False
    try:
        u = urllib.parse.urlparse(url)
        to = u.path.lstrip("/")
        msg = EmailMessage()
        msg["Subject"] = f"[lead-engine] {subject}"
        msg["From"] = urllib.parse.unquote(u.username or "")
        msg["To"] = to
        msg.set_content(body)
        if u.scheme == "smtp+ssl":
            with smtplib.SMTP_SSL(u.hostname, u.port or 465, context=ssl.create_default_context(), timeout=20) as s:
                s.login(urllib.parse.unquote(u.username or ""), urllib.parse.unquote(u.password or ""))
                s.send_message(msg)
        else:
            with smtplib.SMTP(u.hostname, u.port or 587, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(urllib.parse.unquote(u.username or ""), urllib.parse.unquote(u.password or ""))
                s.send_message(msg)
        return True
    except Exception as exc:  # an alert failure must never break the run
        log.error("alert email failed: %s", exc)
        return False
