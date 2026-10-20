"""Sends approved drafts to Instantly (docs/DESIGN.md sections 8, 9 and 18.2).

Order of operations per draft, so nothing is ever sent twice:
  1. in one transaction: re-check suppression and the engineer rule, mark the draft `queued`,
     insert a `sends` row with status=pending; commit.
  2. call Instantly (or write the CSV row).
  3. record the lead ID and mark `sent` (or `exported`).
On restart, `pending` rows are reconciled by looking the lead up before any retry.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import logging

from .. import db, runtime, states, suppression
from ..drafting.drafter import first_name, footer_text
from ..emails.policy import violates_engineer_rule
from .. import http

log = logging.getLogger("lead_engine.send")


class SendBlocked(Exception):
    pass


def to_html(text: str, footer: str = "") -> str:
    def conv(t: str) -> str:
        return html.escape(t.strip()).replace("\r\n", "\n").replace("\n", "<br>")
    out = conv(text)
    if footer:
        out += "<br><br>" + conv(footer)
    return out


def campaign_for(settings, segment: str) -> str:
    cid = settings.get(f"instantly.campaigns.{segment}", "") or ""
    if not cid:
        raise SendBlocked(f"no Instantly campaign configured for group '{segment}'")
    return cid


def pilot_week(settings, today: dt.date | None = None) -> int:
    start = settings.get("sending.pilot_start", "")
    if not start:
        return 1
    today = today or dt.date.today()
    days = (today - dt.date.fromisoformat(start)).days
    return max(1, days // 7 + 1)


def daily_cap(settings, today: dt.date | None = None) -> int:
    ramp = list(settings.get("sending.daily_new_per_inbox", [15, 25, 35, 40]))
    week = pilot_week(settings, today)
    per_inbox = ramp[min(week, len(ramp)) - 1]
    return int(settings.get("sending.inboxes", 0)) * per_inbox


def sent_today(con, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return con.execute("SELECT COUNT(*) n FROM sends WHERE status IN ('sent','exported','pending') "
                       "AND substr(attempted_at,1,10)=?", (today.isoformat(),)).fetchone()["n"]


def active_in_instantly(con) -> int:
    return con.execute("SELECT COUNT(*) n FROM sends WHERE status IN ('sent','exported','pending') "
                       "AND removed_at IS NULL").fetchone()["n"]


def sends_this_month(con, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return con.execute("SELECT COUNT(*) n FROM sends WHERE status IN ('sent','exported') "
                       "AND substr(attempted_at,1,7)=?", (today.strftime("%Y-%m"),)).fetchone()["n"]


def is_paused(con) -> str | None:
    return db.get_cursor(con, "control", "sending_paused")


def preflight(ctx, today: dt.date | None = None) -> int:
    """Raises SendBlocked when sending must not happen. Returns how many new leads may go out now."""
    con, s = ctx.con, ctx.settings
    reason = is_paused(con)
    if reason:
        raise SendBlocked(f"sending is paused: {reason}. Run `lead-engine resume` after checking.")
    last = runtime.last_success(con, "events")
    max_age = float(s.get("sending.events_max_age_hours", 26))
    if not last:
        raise SendBlocked("`events` has never succeeded; run it first so opt-outs are current")
    age_h = (dt.datetime.now(dt.timezone.utc) -
             dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
    if age_h > max_age:
        raise SendBlocked(f"`events` last succeeded {age_h:.0f}h ago (limit {max_age:.0f}h)")
    cap = daily_cap(s, today)
    if cap <= 0:
        raise SendBlocked("sending.inboxes is 0; set the number of warmed-up inboxes in config.toml")
    room_today = cap - sent_today(con, today)
    room_contacts = int(s.get("instantly.contact_limit", 1000)) - active_in_instantly(con)
    room_month = int(s.get("instantly.monthly_email_limit", 5000)) // 3 - sends_this_month(con, today)
    return max(0, min(room_today, room_contacts, room_month))


def resolve_mode(ctx) -> str:
    mode = ctx.settings.get("instantly.mode", "auto")
    if mode in ("api", "csv"):
        return mode
    return "api" if ctx.instantly.ping().get("ok") else "csv"


def _queue(con, d, person, campaign: str, method: str) -> str | None:
    """Step 1. Returns send_id, or None if the draft must not be sent (it is marked stale)."""
    with con:  # one transaction
        cur = con.execute("SELECT state FROM drafts WHERE draft_id=?", (d["draft_id"],)).fetchone()
        if not cur or cur["state"] not in states.SENDABLE:
            return None
        email = person["email"]
        if not email or suppression.is_suppressed(con, email) or violates_engineer_rule(person, email):
            con.execute("UPDATE drafts SET state=?, notes=COALESCE(notes,'') || ?, updated_at=? WHERE draft_id=?",
                        (states.STALE, " [not sent: suppressed or blocked address]", db.now(), d["draft_id"]))
            return None
        send_id = db.new_id("s")
        con.execute("UPDATE drafts SET state=?, updated_at=? WHERE draft_id=?",
                    (states.QUEUED, db.now(), d["draft_id"]))
        con.execute("INSERT INTO sends(send_id,draft_id,campaign_id,email,method,status,attempted_at) "
                    "VALUES(?,?,?,?,?, 'pending', ?)", (send_id, d["draft_id"], campaign, email, method, db.now()))
    return send_id


def _variables(ctx, d, person) -> dict[str, str]:
    text = states.final_text(d)
    footer = footer_text(ctx.settings)
    return {"subject": text["subject"], "body_html": to_html(text["body"], footer),
            "fu1_html": to_html(text["fu1"], footer), "fu2_html": to_html(text["fu2"], footer),
            "first_name": first_name(person["display_name"]), "draft_id": d["draft_id"]}


def _mark(con, send_id: str, draft_id: str, status: str, lead_id: str | None = None, error: str | None = None):
    new_state = {"sent": states.SENT, "exported": states.EXPORTED}.get(status, states.STALE)
    with con:
        con.execute("UPDATE sends SET status=?, instantly_lead_id=COALESCE(?, instantly_lead_id), sent_at=?, "
                    "error=? WHERE send_id=?",
                    (status, lead_id, db.now() if status in ("sent", "exported") else None, error, send_id))
        con.execute("UPDATE drafts SET state=?, updated_at=? WHERE draft_id=?", (new_state, db.now(), draft_id))


def recover_pending(ctx, run) -> int:
    """Reconcile `pending` sends left by a crash: look the lead up before any retry."""
    con = ctx.con
    fixed = 0
    for s in con.execute("SELECT * FROM sends WHERE status='pending' AND method='api'").fetchall():
        lead = ctx.instantly.find_lead(s["campaign_id"], s["email"])
        if lead:
            _mark(con, s["send_id"], s["draft_id"], "sent", lead.get("id"))
        else:
            d = con.execute("SELECT * FROM drafts WHERE draft_id=?", (s["draft_id"],)).fetchone()
            person = db.get_person(con, d["person_id"])
            _push_api(ctx, s["send_id"], d, person, s["campaign_id"], run)
        fixed += 1
    return fixed


def _push_api(ctx, send_id, d, person, campaign, run) -> None:
    try:
        resp = ctx.instantly.add_lead(campaign, person["email"], first_name(person["display_name"]),
                                      _variables(ctx, d, person))
    except http.HTTPError as exc:
        # leave the row pending only for retryable failures; others fail this item
        if exc.status in (0, 429) or exc.status >= 500:
            run.note(f"send {send_id} left pending after HTTP {exc.status}")
            run.failed += 1
            return
        _mark(ctx.con, send_id, d["draft_id"], "failed", error=f"HTTP {exc.status}")
        run.failed += 1
        return
    lead_id = resp.get("id") if isinstance(resp, dict) else None
    if not lead_id or resp.get("status") == "skipped":
        _mark(ctx.con, send_id, d["draft_id"], "failed", error="duplicate")
        run.skipped += 1
        return
    _mark(ctx.con, send_id, d["draft_id"], "sent", lead_id)
    run.ok += 1


CSV_FIELDS = ["email", "first_name", "subject", "body_html", "fu1_html", "fu2_html", "draft_id"]


def send(ctx, run, today: dt.date | None = None, limit: int | None = None) -> dict:
    con = ctx.con
    room = preflight(ctx, today)
    mode = "csv" if ctx.dry_run else resolve_mode(ctx)
    if mode == "api" and not ctx.dry_run:
        recover_pending(ctx, run)
        room = preflight(ctx, today)
    drafts = con.execute("SELECT * FROM drafts WHERE channel='email' AND state IN (?,?) "
                         "ORDER BY decided_at, created_at", tuple(states.SENDABLE)).fetchall()
    budget = min(room, limit) if limit is not None else room
    summary = {"mode": mode, "room": room, "queued": 0, "csv_files": []}
    if ctx.dry_run:
        print(f"[dry-run] {min(len(drafts), budget)} of {len(drafts)} approved drafts would be sent "
              f"(room today: {room})")
        return summary
    csv_rows: dict[str, list[dict]] = {}
    for d in drafts:
        if summary["queued"] >= budget:
            run.note(f"stopped at the daily/contact/monthly limit ({budget}); {len(drafts) - summary['queued']} wait")
            break
        person = db.get_person(con, d["person_id"])
        try:
            campaign = campaign_for(ctx.settings, d["segment"])
        except SendBlocked as exc:
            run.note(str(exc))
            run.skipped += 1
            continue
        send_id = _queue(con, d, person, campaign, mode)
        if not send_id:
            run.skipped += 1
            continue
        summary["queued"] += 1
        if mode == "api":
            _push_api(ctx, send_id, d, person, campaign, run)
        else:
            csv_rows.setdefault(campaign, []).append({"send_id": send_id, "draft_id": d["draft_id"],
                                                      "email": person["email"], **_variables(ctx, d, person)})
    if csv_rows:
        out_dir = ctx.settings.data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for campaign, rows in csv_rows.items():
            path = out_dir / f"instantly_{campaign}_{stamp}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            for r in rows:
                _mark(con, r["send_id"], r["draft_id"], "exported")
                run.ok += 1
            summary["csv_files"].append(str(path))
    return summary


def remove_finished(ctx, run) -> int:
    """Remove leads whose draft is closed from Instantly; history stays in SQLite."""
    con = ctx.con
    rows = con.execute("SELECT s.* FROM sends s JOIN drafts d ON d.draft_id=s.draft_id WHERE d.state=? "
                       "AND s.removed_at IS NULL AND s.status IN ('sent','exported')", (states.CLOSED,)).fetchall()
    removed = 0
    manual: list[str] = []
    for s in rows:
        if ctx.dry_run:
            continue
        if s["method"] == "api" and s["instantly_lead_id"]:
            try:
                ctx.instantly.delete_lead(s["instantly_lead_id"])
            except http.HTTPError as exc:
                run.note(f"could not remove lead {s['instantly_lead_id']}: HTTP {exc.status}")
                continue
        else:
            manual.append(s["email"])
        con.execute("UPDATE sends SET removed_at=? WHERE send_id=?", (db.now(), s["send_id"]))
        removed += 1
    con.commit()
    if manual:
        out = ctx.settings.data_dir / "exports" / f"remove-from-instantly-{dt.date.today().isoformat()}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for e in manual:
                w.writerow([e])
        run.note(f"CSV mode: remove {len(manual)} finished leads from Instantly by hand (listed in {out.name})")
    return removed
