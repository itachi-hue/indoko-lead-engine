"""Pipeline commands. Imported by cli.py, which registers them."""
from __future__ import annotations

import json
from pathlib import Path

from . import db, states
from .cli import command, configure
from .config import ALL_SEGMENTS


# ---------------- warm list ----------------
@command("import-warm", "Import the warm list (Excel on Drive, or a local file)")
def cmd_import_warm(ctx, args, run):
    from .sources import warm

    if args.file:
        if not args.consent_confirmed:
            print("refusing to import: pass --consent-confirmed once you've confirmed these people agreed "
                  "to hear from Indoko")
            return 2
        rows = warm.read_rows(Path(args.file).read_bytes())
        res = warm.import_rows(ctx.con, rows, args.label or Path(args.file).stem, args.assume_us,
                               bool(ctx.settings.get("pilot.us_only", True)))
        run.ok += res.imported
        print(json.dumps(res.__dict__, indent=2))
        return 0
    for label, res in warm.import_drive_files(ctx, run):
        print(label, json.dumps(res.__dict__))
    return 0


@configure("import-warm")
def _(p):
    p.add_argument("--file", help="local .xlsx instead of the Drive files in config")
    p.add_argument("--label", help="name for this list (used in provenance)")
    p.add_argument("--assume-us", action="store_true", help="treat rows without a location as US-based")
    p.add_argument("--consent-confirmed", action="store_true")


# ---------------- drafting ----------------
@command("draft", "Draft emails for one group")
def cmd_draft(ctx, args, run):
    from .drafting.drafter import draft_batch

    limit = args.limit or int(ctx.settings.get("pilot.per_group_limit", 100))
    if ctx.dry_run:
        from .drafting.drafter import eligible_people
        n = len(eligible_people(ctx.con, args.group, limit, bool(ctx.settings.get("pilot.us_only", True))))
        print(f"[dry-run] {n} people would be drafted for '{args.group}' (Gemini is not called)")
        return 0
    batch = draft_batch(ctx, run, args.group, limit)
    print(f"batch {batch}: {run.ok} drafted, {run.skipped} need a hand-written email, {run.failed} failed")
    return 0


@configure("draft")
def _(p):
    p.add_argument("--group", required=True, choices=ALL_SEGMENTS)
    p.add_argument("--limit", type=int)


# ---------------- review ----------------
@command("review", "Sync drafts to and from the Review tab")
def cmd_review(ctx, args, run):
    from .sheets import review

    if args.action == "push":
        n = review.push(ctx, run)
        print(f"added {n} rows to the Review tab")
    else:
        counts = review.pull(ctx, run)
        review.protect_locked_rows(ctx)
        print(json.dumps(counts))
    return 0


@configure("review")
def _(p):
    p.add_argument("action", choices=["push", "pull"])


# ---------------- sending ----------------
@command("send", "Send approved drafts to Instantly (API or CSV)")
def cmd_send(ctx, args, run):
    from .instantly import sender

    try:
        summary = sender.send(ctx, run, limit=args.limit)
    except sender.SendBlocked as exc:
        print(f"not sending: {exc}")
        run.note(str(exc))
        return 1
    print(json.dumps(summary, indent=2))
    return 0


@configure("send")
def _(p):
    p.add_argument("--limit", type=int, help="send at most this many now")


@command("events", "Pull replies, bounces and unsubscribes from Instantly")
def cmd_events(ctx, args, run):
    from .instantly import events, sender

    if args.from_csv:
        res = events.import_csv(ctx, run, Path(args.from_csv).read_text(encoding="utf-8"))
    else:
        mode = ctx.settings.get("instantly.mode", "auto")
        if mode == "csv" or (mode == "auto" and not ctx.instantly.ping().get("ok")):
            print("CSV mode: export the campaign lead lists from Instantly and run "
                  "`lead-engine events --from-csv <file>`. Sending stays blocked until that import succeeds.")
            run.note("CSV mode: no events imported")
            return 1
        else:
            res = events.poll_api(ctx, run)
    paused = events.bounce_guard(ctx, run)
    removed = sender.remove_finished(ctx, run)
    flags = {pid: msg for pid, msg in res.get("flagged", [])}
    if flags:
        db.set_cursor(ctx.con, "control", "crm_flags", json.dumps(flags))
        ctx.con.commit()
    print(json.dumps({"suppressed": res["suppressed"], "flagged": len(flags), "paused": paused,
                      "removed_from_instantly": removed}))
    return 0


@configure("events")
def _(p):
    p.add_argument("--from-csv", help="import an Instantly lead-list export instead of polling the API")


@command("resume", "Clear a sending pause after checking the cause")
def cmd_resume(ctx, args, run):
    reason = db.get_cursor(ctx.con, "control", "sending_paused")
    if not reason:
        print("sending is not paused")
        return 0
    ctx.con.execute("DELETE FROM crawl_cursors WHERE source='control' AND ckey='sending_paused'")
    ctx.con.commit()
    print(f"cleared pause ({reason}). Resume the campaigns in Instantly too.")
    return 0


# ---------------- CRM ----------------
@command("crm", "Sync the CRM tab (push pipeline fields, pull team fields)")
def cmd_crm(ctx, args, run):
    from .sheets import crm

    if args.action == "pull":
        print(json.dumps(crm.pull(ctx, run)))
    else:
        flags = json.loads(db.get_cursor(ctx.con, "control", "crm_flags") or "{}")
        print(json.dumps(crm.push(ctx, run, flags)))
    return 0


@configure("crm")
def _(p):
    p.add_argument("action", choices=["push", "pull"])


# ---------------- privacy ----------------
@command("forget", "Delete a person everywhere; keep a hashed suppression entry")
def cmd_forget(ctx, args, run):
    from .forget import forget

    print(json.dumps(forget(ctx, run, args.email)))
    return 0


@configure("forget")
def _(p):
    p.add_argument("email")


@command("suppress", "Add an email or domain to the suppression list")
def cmd_suppress(ctx, args, run):
    from . import suppression

    v = args.value.strip().lower()
    kind = "email" if "@" in v and not v.startswith("@") else "domain"
    added = suppression.add(ctx.con, kind, v.lstrip("@"), args.reason, "manual")
    ctx.con.execute("UPDATE drafts SET state=?, updated_at=? WHERE state IN (?,?,?,?,?) AND person_id IN "
                    "(SELECT person_id FROM persons WHERE lower(email)=? OR lower(email) LIKE ?)",
                    (states.STALE, db.now(), states.DRAFTED, states.NEEDS_MANUAL, states.PUSHED, states.APPROVED,
                     states.EDITED, v, f"%@{v.lstrip('@')}"))
    ctx.con.commit()
    print("added" if added else "already suppressed")
    return 0


@configure("suppress")
def _(p):
    p.add_argument("value", help="email address or domain")
    p.add_argument("--reason", default="manual")


# ---------------- daily run ----------------
@command("run-daily", "events → review pull → send → crm pull/push")
def cmd_run_daily(ctx, args, run):
    """Each step is recorded as its own run; a failure in one step stops the steps after it."""
    from .runtime import Run

    steps = [("events", cmd_events, {"from_csv": None}),
             ("review", cmd_review, {"action": "pull"}),
             ("send", cmd_send, {"limit": None}),
             ("crm", cmd_crm, {"action": "pull"}),
             ("crm", cmd_crm, {"action": "push"})]
    for name, fn, kw in steps:
        sub_args = type("Args", (), kw)()
        with Run(ctx.con, name) as sub:
            code = fn(ctx, sub_args, sub) or 0
            sub.exit_code = code
        if name == "events" and code != 0:
            run.note("events failed; not sending")
            return code
        if code not in (0, 1):
            return code
    run.ok += 1
    return 0


# ---------------- cold sourcing and scoring ----------------
@command("source", "Find people for one cold group (Exa, or OpenAlex for researchers)")
def cmd_source(ctx, args, run):
    from .sources import cold

    limit = args.limit or int(ctx.settings.get("pilot.per_group_limit", 100))
    if ctx.dry_run:
        print(f"[dry-run] would source up to {limit} '{args.group}' people (no API calls made)")
        return 0
    n = cold.source(ctx, run, args.group, limit, args.field)
    print(f"{args.group}: {n} people added or updated (cost ~${run.cost_usd:.2f}, skipped {run.skipped})")
    return 0


@configure("source")
def _(p):
    p.add_argument("--group", required=True, choices=["engineer", "founder", "researcher", "creator"])
    p.add_argument("--limit", type=int)
    p.add_argument("--field", help="researchers only: e.g. computer-science, biology, physics")


@command("score", "Score everyone in the cold groups (achievements only)")
def cmd_score(ctx, args, run):
    from . import scoring

    counts = scoring.score_all(ctx.con)
    run.ok += sum(counts.values())
    ready = {}
    for seg in counts:
        rows = ctx.con.execute("SELECT person_id FROM persons WHERE segment=? AND us_located=1", (seg,)).fetchall()
        ready[seg] = sum(1 for r in rows if scoring.evidence_ok(ctx.con, r["person_id"]))
    print(json.dumps({"scored": counts, "us_with_2plus_dated_evidence": ready}, indent=2))
    return 0


# ---------------- email finding ----------------
@command("find-emails", "Find and verify one usable email per person, within a dollar budget")
def cmd_find_emails(ctx, args, run):
    from .emails.find import candidates_for, find_emails

    limit = args.limit or int(ctx.settings.get("pilot.per_group_limit", 100))
    budget = args.budget if args.budget is not None else float(ctx.settings.get("emails.budget_usd_per_run", 10))
    if ctx.dry_run:
        n = len(candidates_for(ctx.con, args.group, limit))
        print(f"[dry-run] {n} '{args.group}' people need an email; budget ${budget:.2f}; no lookups made")
        return 0
    print(json.dumps(find_emails(ctx, run, args.group, limit, budget)))
    return 0


@configure("find-emails")
def _(p):
    p.add_argument("--group", required=True, choices=["engineer", "founder", "researcher", "creator"])
    p.add_argument("--limit", type=int)
    p.add_argument("--budget", type=float, help="dollars for this run (default from config)")


# ---------------- LinkedIn list (engineers without a personal email) ----------------
@command("linkedin", "Prepare connection notes for engineers with no usable email, and add them to the LinkedIn tab")
def cmd_linkedin(ctx, args, run):
    from .drafting import linkedin

    if ctx.dry_run:
        print(f"[dry-run] {len(linkedin.eligible(ctx.con, args.limit))} engineers would get a note")
        return 0
    made = linkedin.prepare(ctx, run, args.limit)
    pushed = linkedin.push(ctx, run)
    print(f"prepared {made} notes, added {pushed} rows to the LinkedIn tab")
    return 0


@configure("linkedin")
def _(p):
    p.add_argument("--limit", type=int, default=50)
