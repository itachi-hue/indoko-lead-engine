"""The single suppression list. Checked at import, before drafting, and before sending.

Matches exact emails, whole domains, and sha256 hashes of forgotten addresses.
"""
from __future__ import annotations

import hashlib
import re

from . import db

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
ROLE_LOCALPARTS = {"info", "hello", "contact", "admin", "support", "sales", "team", "office", "press",
                   "media", "jobs", "careers", "hr", "noreply", "no-reply", "help", "marketing", "billing"}


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    e = value.strip().strip("<>").strip().lower()
    if e.startswith("mailto:"):
        e = e[7:]
    return e if EMAIL_RE.match(e) else None


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def is_role_address(email: str) -> bool:
    return email.split("@", 1)[0].lower() in ROLE_LOCALPARTS


def add(con, match_type: str, value: str, reason: str, source: str) -> bool:
    """Add an entry. Returns False if it was already present."""
    if match_type not in ("email", "domain", "email_hash"):
        raise ValueError(match_type)
    value = value.strip().lower()
    cur = con.execute("INSERT INTO suppression(supp_id,match_type,value,reason,source,added_at) "
                      "VALUES(?,?,?,?,?,?) ON CONFLICT(match_type,value) DO NOTHING",
                      (db.stable_id("supp", match_type, value), match_type, value, reason, source, db.now()))
    return cur.rowcount > 0


def add_email(con, email: str, reason: str, source: str) -> bool:
    e = normalize_email(email) or email.strip().lower()
    return add(con, "email", e, reason, source)


def parent_domains(domain: str) -> list[str]:
    """eu.acme.com -> [eu.acme.com, acme.com] (never a bare TLD)."""
    parts = domain.lower().split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


def is_suppressed(con, email: str | None) -> bool:
    e = normalize_email(email)
    if not e:
        return False
    doms = parent_domains(domain_of(e))
    r = con.execute(
        f"SELECT 1 FROM suppression WHERE (match_type='email' AND value=?) OR (match_type='email_hash' AND value=?) "
        f"OR (match_type='domain' AND value IN ({','.join('?' * len(doms))})) LIMIT 1",
        (e, email_hash(e), *doms)).fetchone()
    return r is not None


def stop_in_flight(con) -> int:
    """Mark every not-yet-sent draft whose address is now suppressed as stale, and fail its pending send.
    Call after adding suppression entries."""
    from . import states  # local import avoids a cycle

    stopped = 0
    rows = con.execute(
        "SELECT d.draft_id, p.email, s.send_id FROM drafts d JOIN persons p ON p.person_id=d.person_id "
        "LEFT JOIN sends s ON s.draft_id=d.draft_id AND s.status='pending' "
        f"WHERE d.state IN ({','.join('?' * 6)}) AND p.email IS NOT NULL",
        (states.DRAFTED, states.NEEDS_MANUAL, states.PUSHED, states.APPROVED, states.EDITED,
         states.QUEUED)).fetchall()
    for r in rows:
        if is_suppressed(con, r["email"]):
            con.execute("UPDATE drafts SET state=?, notes=COALESCE(notes,'') || ' [not sent: suppressed]', "
                        "updated_at=? WHERE draft_id=?", (states.STALE, db.now(), r["draft_id"]))
            if r["send_id"]:
                con.execute("UPDATE sends SET status='failed', error='suppressed' WHERE send_id=?", (r["send_id"],))
            stopped += 1
    return stopped


def entries(con, sources: tuple[str, ...] | None = None) -> list[dict]:
    q = "SELECT match_type, value, reason, source, added_at FROM suppression"
    args: tuple = ()
    if sources:
        q += f" WHERE source IN ({','.join('?' * len(sources))})"
        args = sources
    return [dict(r) for r in con.execute(q + " ORDER BY added_at", args)]
