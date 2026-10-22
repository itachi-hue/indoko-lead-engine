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
        n = len(eligible_people(ctx.con, args.group, limit, bool(ctx.settings.get("pilot.us_only", True)),
                                require_moved=bool(ctx.settings.get("pilot.draft_only_movers", False))))
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
    new_flags = {pid: msg for pid, msg in res.get("flagged", [])}
    if new_flags:  # merge: a flag stays until the team tags a reply reason for that person
        flags = json.loads(db.get_cursor(ctx.con, "control", "crm_flags") or "{}")
        flags.update(new_flags)
        db.set_cursor(ctx.con, "control", "crm_flags", json.dumps(flags))
        ctx.con.commit()
    flags = new_flags
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
        flags = json.loads(db.get_cursor(ctx.con, "control", "crm_flags") or "{}")
        tagged = {r["ckey"] for r in ctx.con.execute("SELECT ckey FROM crawl_cursors WHERE source='reply_reason'")}
        if any(pid in tagged for pid in flags):
            db.set_cursor(ctx.con, "control", "crm_flags",
                          json.dumps({k: v for k, v in flags.items() if k not in tagged}))
            ctx.con.commit()
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
    stopped = suppression.stop_in_flight(ctx.con)
    ctx.con.commit()
    print(("added" if added else "already suppressed") + f"; {stopped} unsent drafts stopped")
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
    if args.save_articles or args.extractions:
        if args.group not in cold.ARTICLE_GROUPS:
            print("--save-articles / --extractions apply to founders and creators only")
            return 2
        out_dir = ctx.settings.data_dir / "extract"
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.save_articles:
            arts = cold.fetch_articles(ctx, run, args.group, args.max_articles or limit * 3)
            path = out_dir / f"{args.group}-articles-{db.now()[:10]}.json"
            path.write_text(json.dumps(arts, indent=1))
            print(f"saved {len(arts)} articles to {path} (cost ~${run.cost_usd:.2f}). Write extractions as "
                  f"{{url: extraction}} and run: source --group {args.group} --articles {path} --extractions <file>")
            return 0
        arts = json.loads(Path(args.articles).read_text())
        extracted = json.loads(Path(args.extractions).read_text())
        n = cold.ingest_articles(ctx, run, args.group, arts, limit, extracted)
        print(f"{args.group}: {n} people imported from {len(arts)} articles ({run.skipped} articles yielded nobody verifiable)")
        return 0
    n = cold.source(ctx, run, args.group, limit, args.field, args.via)
    print(f"{args.group}: {n} people added or updated (cost ~${run.cost_usd:.2f}, skipped {run.skipped})")
    return 0


@configure("source")
def _(p):
    p.add_argument("--group", required=True, choices=["engineer", "founder", "researcher", "creator"])
    p.add_argument("--limit", type=int)
    p.add_argument("--field", help="researchers only: e.g. computer-science, biology, physics")
    p.add_argument("--via", choices=["patents", "sec_form_d", "exa", "exa_people"],
                   help="override the source (defaults are free: patents for engineers, SEC Form D for founders)")
    p.add_argument("--save-articles", action="store_true",
                   help="founders/creators: fetch news articles and save them for outside extraction")
    p.add_argument("--max-articles", type=int)
    p.add_argument("--articles", help="founders/creators: saved articles file (with --extractions)")
    p.add_argument("--extractions", help="founders/creators: JSON {article url: extraction} to verify and import")


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
    print(json.dumps(find_emails(ctx, run, args.group, limit, budget, args.discover_only), indent=1))
    return 0


@configure("find-emails")
def _(p):
    p.add_argument("--group", required=True, choices=["engineer", "founder", "researcher", "creator"])
    p.add_argument("--limit", type=int)
    p.add_argument("--budget", type=float, help="dollars for this run (default from config)")
    p.add_argument("--discover-only", action="store_true",
                   help="no verifier yet: collect unverified candidates without marking anyone ready to email")


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


# ---------------- pilot operations ----------------
@command("report", "Write weekly funnel metrics to the Metrics tab")
def cmd_report(ctx, args, run):
    from . import report

    n = report.write(ctx, run)
    print(json.dumps({"rows": n, "reply_reasons": report.reply_reasons(ctx.con)}, indent=2))
    return 0


@command("backup", "Encrypt the database with age and upload it to the Shared Drive folder")
def cmd_backup(ctx, args, run):
    from . import maintenance

    try:
        print(json.dumps(maintenance.backup(ctx, run)))
    except maintenance.BackupError as exc:
        print(f"backup not made: {exc}")
        run.note(str(exc))
        return 1
    return 0


@command("retention", "Delete people never contacted after the retention window (default 90 days)")
def cmd_retention(ctx, args, run):
    from . import maintenance

    print(f"deleted {maintenance.retention(ctx, run)} never-contacted people")
    return 0


# ---------------- evaluation (M7) ----------------
@command("eval", "Evaluation: sample, score, checks, invariants, pilot (docs/DESIGN.md section 18)")
def cmd_eval(ctx, args, run):
    from .eval import harness

    if args.action == "sample":
        path = harness.sample(ctx, args.stage, args.n, args.seed)
        print(f"labelling sheet: {path}\nFill in the label_ columns, then: lead-engine eval score {args.stage} {path}")
        return 0
    if args.action == "score":
        res = harness.score(ctx, args.stage, Path(args.file))
    elif args.action == "checks":
        res = harness.checks_eval()
    elif args.action == "invariants":
        res = harness.invariants(ctx)
    else:
        res = harness.pilot(ctx)
    harness.save(ctx, res)
    print(json.dumps(res, indent=2))
    return 0 if res.get("pass", True) else 1


@configure("eval")
def _(p):
    p.add_argument("action", choices=["sample", "score", "checks", "invariants", "pilot"])
    p.add_argument("stage", nargs="?", choices=["sourcing", "dedupe", "evidence", "scoring", "emails", "drafts"])
    p.add_argument("file", nargs="?")
    p.add_argument("--n", type=int)
    p.add_argument("--seed", type=int)


# ---------------- which engineer employers can have guessed addresses confirmed ----------------
@command("check-domains", "Find which engineer employers' mail servers confirm mailboxes (1 check each)")
def cmd_check_domains(ctx, args, run):
    from .emails import verify as verify_mod
    from .emails.find import _verify
    from .sources.cold import COMPANIES

    skip = {"Google", "Meta", "Amazon"}
    names = [c for c in COMPANIES if c not in skip and (not args.only or c in args.only)]
    if ctx.dry_run:
        print(f"[dry-run] would check {len(names)} domains with the configured verifier")
        return 0
    verifier = verify_mod.build(ctx)
    if verifier is None:
        raise RuntimeError("no email verifier configured (emails.verifier + VERIFIER_API_KEY)")
    results = {}
    for name in names:
        # an address nobody has: a server that says "deliverable" or "catch_all" accepts everything
        probe = f"zq-no-such-person-7319@{COMPANIES[name]}"
        results[name] = _verify(ctx.con, verifier, probe, None, run)
        ctx.con.commit()
    good = [n for n, st in results.items() if st == "undeliverable"]
    for n, st in sorted(results.items(), key=lambda kv: kv[1]):
        verdict = "confirms mailboxes" if st == "undeliverable" else (
            "accepts every address" if st in ("catch_all", "deliverable") else "no answer")
        print(f"{n:14} {COMPANIES[n]:20} {verdict}")
    print("\n[sourcing]\nengineer_companies = " + json.dumps(good))
    run.ok = len(good)
    return 0


@configure("check-domains")
def _(p):
    p.add_argument("--only", nargs="*", help="company names to check (default: all except Google, Meta, Amazon)")


# ---------------- sourcing pipeline (find → emails → drafts → Review tab) ----------------
@command("pipeline", "For each group: source → find emails → draft → push to the Review tab")
def cmd_pipeline(ctx, args, run):
    """Each step is its own recorded run. Steps whose keys aren't set yet are skipped with a note,
    so the pipeline can run today (sourcing + emails) and grows as keys are added."""
    from .runtime import Run

    groups = args.groups or list(ctx.settings.get("pilot.groups", ["engineer", "founder", "researcher"]))
    limit = args.limit or int(ctx.settings.get("pilot.per_group_limit", 100))
    have_verifier = bool(ctx.settings.get("emails.verifier")) and (
        bool(ctx.settings.secret("VERIFIER_API_KEY")) or str(ctx.settings.get("emails.verifier")).startswith("reacher"))
    have_gemini = bool(ctx.settings.secret("GEMINI_API_KEY"))
    have_sheet = bool(ctx.settings.get("sheet.spreadsheet_id"))
    plan = []
    for g in groups:
        plan.append(("source", cmd_source, {"group": g, "limit": limit, "field": None, "via": None,
                                            "save_articles": False, "extractions": None, "articles": None,
                                            "max_articles": None}))
        if have_verifier:
            plan.append(("find-emails", cmd_find_emails, {"group": g, "limit": limit * 2, "budget": args.budget,
                                                          "discover_only": False}))
        else:
            plan.append(("find-emails", cmd_find_emails, {"group": g, "limit": limit * 2, "budget": args.budget,
                                                          "discover_only": True}))
        if have_gemini and have_verifier and not args.no_draft:
            plan.append(("draft", cmd_draft, {"group": g, "limit": limit}))
    if have_gemini and have_sheet and have_verifier and not args.no_draft:
        plan.append(("review", cmd_review, {"action": "push"}))
    skipped = []
    if not have_verifier:
        skipped.append("email checks (no VERIFIER_API_KEY): candidates are collected, nobody is marked ready")
    if not have_gemini:
        skipped.append("drafts (no GEMINI_API_KEY)")
    if not have_sheet:
        skipped.append("Review tab (no sheet.spreadsheet_id)")
    for note in skipped:
        print(f"skipping {note}")
        run.note(f"skipped {note}")
    for name, fn, kw in plan:
        print(f"--> {name} " + " ".join(f"{k}={v}" for k, v in kw.items() if k in ("group", "limit", "action")))
        with Run(ctx.con, name) as sub:
            code = fn(ctx, type("Args", (), kw)(), sub) or 0
            sub.exit_code = code
            run.cost_usd += sub.cost_usd
        if code not in (0, 1):
            run.note(f"{name} failed with exit code {code}; stopping")
            return code
    run.ok += 1
    print(f"pipeline done; cost ~${run.cost_usd:.2f}")
    return 0


@configure("pipeline")
def _(p):
    p.add_argument("--groups", nargs="*", choices=["engineer", "founder", "researcher"])
    p.add_argument("--limit", type=int, help="people to source per group (default pilot.per_group_limit)")
    p.add_argument("--budget", type=float, help="email-finding dollars per group")
    p.add_argument("--no-draft", action="store_true", help="stop after emails")
