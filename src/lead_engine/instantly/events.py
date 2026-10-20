"""Pulls what happened in Instantly back into `events` (docs/DESIGN.md sections 8 and 9).

API mode polls leads and received emails. CSV mode imports a lead-list export. Either way:
unsubscribes and hard bounces go to suppression (and Instantly's blocklist) on the same run,
finished leads close their draft, and the bounce rate can pause sending.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re

from .. import alerts, db, states, suppression
from .. import http

log = logging.getLogger("lead_engine.events")

# ---- Instantly lead fields (verify in M0; see docs/DESIGN.md section 16) ----
# Lead `status`: 1 active, 2 paused, 3 completed, -1 bounced, -2 unsubscribed, -3 skipped.
LEAD_STATUS = {3: "completed", -1: "bounce", -2: "unsubscribe"}
# Lead `lt_interest_status` set in Instantly's inbox: 1 interested, 2 meeting booked,
# 3 meeting completed, 4 won, 0 out of office, -1 not interested, -2 wrong person, -3 lost.
INTEREST = {1: "positive_reply", 2: "positive_reply", 3: "positive_reply", 4: "positive_reply",
            -1: "not_interested", -2: "not_interested", -3: "not_interested"}

UNSUB_RE = re.compile(r"\b(unsubscribe|remove me|remove|stop|opt[- ]?out|take me off)\b", re.I)
AMBIGUOUS_RE = re.compile(r"\b(not interested|no thanks|don'?t (?:email|contact))\b", re.I)


QUOTE_START = re.compile(r"^\s*(>|on .{0,120} wrote:|-{2,}\s*original message|from:\s)", re.I | re.M)


def strip_quoted(text: str) -> str:
    """Drop the quoted original email (it contains our footer, which mentions unsubscribing)."""
    m = QUOTE_START.search(text or "")
    return (text or "")[: m.start()] if m else (text or "")


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|<blockquote", r"\n\g<0>", text)
    text = re.sub(r"(?is)<blockquote.*", "\n> ", text)          # quoted original starts here
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def reply_text(em: dict) -> str:
    body = em.get("body")
    if isinstance(body, dict):
        return body.get("text") or html_to_text(body.get("html") or "")
    return body or ""


def classify_reply(text: str) -> str:
    """'unsubscribe' if the first 200 characters ask to stop; 'ambiguous' for soft no's; else 'reply'."""
    head = strip_quoted(text)[:200]
    if UNSUB_RE.search(head):
        return "unsubscribe"
    if AMBIGUOUS_RE.search(head):
        return "ambiguous"
    return "reply"


def _send_by_email(con, email: str):
    return con.execute("SELECT s.*, d.person_id FROM sends s JOIN drafts d ON d.draft_id=s.draft_id "
                       "WHERE lower(s.email)=? ORDER BY s.attempted_at DESC LIMIT 1",
                       (email.lower(),)).fetchone()


def _close_draft(con, draft_id: str) -> None:
    con.execute("UPDATE drafts SET state=?, updated_at=? WHERE draft_id=? AND state IN (?,?)",
                (states.CLOSED, db.now(), draft_id, states.SENT, states.EXPORTED))


def _stale_other_drafts(con, person_id: str) -> None:
    con.execute("UPDATE drafts SET state=?, updated_at=? WHERE person_id=? AND state IN (?,?,?,?,?)",
                (states.STALE, db.now(), person_id, states.DRAFTED, states.NEEDS_MANUAL, states.PUSHED,
                 states.APPROVED, states.EDITED))


class EventSink:
    """Applies events consistently, whichever mode they came from."""

    def __init__(self, ctx, run, mode: str):
        self.ctx, self.con, self.run, self.mode = ctx, ctx.con, run, mode
        self.new_suppressed: list[str] = []
        self.flagged: list[tuple[str, str]] = []

    def record(self, send_row, type_: str, external_id: str, occurred_at: str | None = None, payload=None):
        added = db.add_event(self.con, send_row["person_id"], type_, "instantly" if self.mode == "api" else "csv",
                             occurred_at=occurred_at, draft_id=send_row["draft_id"], external_id=external_id,
                             payload=payload)
        if not added:
            return
        self.run.ok += 1
        if type_ in ("unsubscribe", "bounce"):
            reason = "unsubscribed" if type_ == "unsubscribe" else "hard bounce"
            if suppression.add_email(self.con, send_row["email"], reason, "instantly"):
                self.new_suppressed.append(send_row["email"])
            _stale_other_drafts(self.con, send_row["person_id"])
            _close_draft(self.con, send_row["draft_id"])
        elif type_ == "not_interested":
            _close_draft(self.con, send_row["draft_id"])

    def finish(self):
        if self.new_suppressed:
            suppression.stop_in_flight(self.con)
        self.con.commit()
        if self.new_suppressed:
            if self.mode == "api" and not self.ctx.dry_run:
                failed = []
                for e in self.new_suppressed:
                    try:
                        self.ctx.instantly.block(e)
                    except http.HTTPError as exc:
                        failed.append(e)
                        self.run.note(f"blocklist add failed (HTTP {exc.status}); listed in suppression-export.csv")
                if failed:
                    write_suppression_export(self.ctx, failed)
            else:
                write_suppression_export(self.ctx, self.new_suppressed)


def write_suppression_export(ctx, emails: list[str]) -> None:
    out = ctx.settings.data_dir / "exports" / "suppression-export.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    new_file = not out.exists()
    with out.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["email", "added"])
        for e in emails:
            w.writerow([e, dt.date.today().isoformat()])


# ---------------- API mode ----------------
def _ts(v) -> str | None:
    return v if isinstance(v, str) and v else None


def poll_api(ctx, run) -> dict:
    con = ctx.con
    sink = EventSink(ctx, run, "api")
    campaigns = {c for c in (ctx.settings.get("instantly.campaigns", {}) or {}).values() if c}
    for cid in campaigns:
        for lead in ctx.instantly.list_leads(cid):
            email = (lead.get("email") or "").lower()
            s = _send_by_email(con, email) if email else None
            if not s:
                continue
            lid = lead.get("id") or s["instantly_lead_id"] or email
            if _ts(lead.get("timestamp_last_contact")):
                sink.record(s, "sent", f"sent:{lid}", _ts(lead.get("timestamp_last_contact")))
            if (lead.get("email_reply_count") or 0) > 0:
                sink.record(s, "reply", f"reply:{lid}", _ts(lead.get("timestamp_last_reply")))
            status = LEAD_STATUS.get(lead.get("status"))
            if status in ("bounce", "unsubscribe"):
                sink.record(s, status, f"{status}:{lid}")
            elif status == "completed":
                _close_draft(con, s["draft_id"])
            interest = INTEREST.get(lead.get("lt_interest_status"))
            if interest:
                sink.record(s, interest, f"{interest}:{lid}")
    since = db.get_cursor(con, "instantly", "emails_since")
    newest = since
    for em in ctx.instantly.list_received_emails(since):
        email = (em.get("from_address_email") or em.get("lead") or "").lower()
        s = _send_by_email(con, email) if email else None
        if not s:
            continue
        text = reply_text(em)
        kind = classify_reply(text or "")
        eid = em.get("id") or f"{email}:{em.get('timestamp_created')}"
        if kind == "unsubscribe":
            sink.record(s, "unsubscribe", f"unsubreply:{eid}", _ts(em.get("timestamp_created")))
        else:
            sink.record(s, "reply", f"replymsg:{eid}", _ts(em.get("timestamp_created")))
            if kind == "ambiguous":
                sink.flagged.append((s["person_id"], "Reply may be a no or an opt-out; please check"))
        ts = _ts(em.get("timestamp_created"))
        if ts and (not newest or ts > newest):
            newest = ts
    if newest:
        db.set_cursor(con, "instantly", "emails_since", newest)
    sink.finish()
    return {"suppressed": len(sink.new_suppressed), "flagged": sink.flagged}


# ---------------- CSV mode ----------------
CSV_ALIASES = {
    "email": ["email", "lead email", "email address"],
    "status": ["status", "lead status"],
    "interest": ["interest status", "lt interest status", "interest"],
    "replies": ["reply count", "replies", "email reply count"],
    "last_contact": ["last contacted", "timestamp last contact", "last contact"],
    "last_reply": ["last replied", "timestamp last reply", "last reply"],
}
CSV_STATUS = {"bounced": "bounce", "unsubscribed": "unsubscribe", "completed": "completed",
              "-1": "bounce", "-2": "unsubscribe", "3": "completed"}
CSV_INTEREST = {"interested": "positive_reply", "meeting booked": "positive_reply",
                "meeting completed": "positive_reply", "won": "positive_reply",
                "not interested": "not_interested", "wrong person": "not_interested", "lost": "not_interested"}


def import_csv(ctx, run, text: str) -> dict:
    con = ctx.con
    sink = EventSink(ctx, run, "csv")
    reader = csv.DictReader(io.StringIO(text))
    header = {h: h.strip().lower().replace("_", " ") for h in (reader.fieldnames or [])}
    col = {}
    for key, aliases in CSV_ALIASES.items():
        for raw, norm in header.items():
            if norm in aliases:
                col[key] = raw
                break
    if "email" not in col:
        raise ValueError("the CSV has no email column")
    for row in reader:
        email = (row.get(col["email"]) or "").strip().lower()
        s = _send_by_email(con, email) if email else None
        if not s:
            continue
        get = lambda k: (row.get(col[k]) or "").strip() if k in col else ""  # noqa: E731
        if get("last_contact"):
            sink.record(s, "sent", f"sent:{email}:{s['send_id']}", None)
        if get("replies") and get("replies") not in ("0", ""):
            sink.record(s, "reply", f"reply:{email}:{s['send_id']}", None)
        st = CSV_STATUS.get(get("status").lower())
        if st in ("bounce", "unsubscribe"):
            sink.record(s, st, f"{st}:{email}:{s['send_id']}")
        elif st == "completed":
            _close_draft(con, s["draft_id"])
        it = CSV_INTEREST.get(get("interest").lower())
        if it:
            sink.record(s, it, f"{it}:{email}:{s['send_id']}")
    sink.finish()
    return {"suppressed": len(sink.new_suppressed), "flagged": sink.flagged}


# ---------------- bounce guard ----------------
def bounce_rate_7d(con) -> tuple[int, int]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sends = con.execute("SELECT COUNT(*) n FROM sends WHERE status IN ('sent','exported') AND sent_at >= ?",
                        (since,)).fetchone()["n"]
    bounces = con.execute("SELECT COUNT(*) n FROM events WHERE type='bounce' AND occurred_at >= ?",
                          (since,)).fetchone()["n"]
    return sends, bounces


def bounce_guard(ctx, run) -> bool:
    """Pause sending when the 7-day hard-bounce rate is over the limit (once 50+ sends). True if paused."""
    con, s = ctx.con, ctx.settings
    sends, bounces = bounce_rate_7d(con)
    min_sends = int(s.get("sending.bounce_min_sends", 50))
    limit = float(s.get("sending.bounce_pause_pct", 2.0))
    if sends < min_sends:
        return False
    rate = 100.0 * bounces / sends
    if rate <= limit:
        return False
    reason = f"7-day bounce rate {rate:.1f}% over {limit}% ({bounces}/{sends})"
    db.set_cursor(con, "control", "sending_paused", reason)
    con.commit()
    if not ctx.dry_run:
        for cid in {c for c in (s.get("instantly.campaigns", {}) or {}).values() if c}:
            try:
                ctx.instantly.pause_campaign(cid)
            except Exception as exc:  # pausing is best effort; the local pause flag already blocks sends
                run.note(f"could not pause campaign {cid} in Instantly: {exc}")
        alerts.send(ctx, "Sending paused", reason)
    run.note(reason)
    return True
