"""Review tab sync (docs/DESIGN.md sections 5 and 8).

push: new drafts (drafted, needs_manual) become rows; drafts still in `pushed` whose row was
      deleted are added back.
pull: reads the whole tab once (one snapshot), then applies each decision only if the draft is
      still `pushed` in the database. Edits are re-checked; failures go back to `pushed` with the
      reason in Notes. Rows for queued or later drafts are protected and edits to them are ignored.
"""
from __future__ import annotations

import json
import logging

from .. import db, states
from ..drafting import checks
from ..drafting.drafter import check_context, evidence_pack, footer_text
from .layout import REVIEW_COLUMNS, col_letter

log = logging.getLogger("lead_engine.review")
C = {name: i for i, name in enumerate(REVIEW_COLUMNS)}
LOCKED_STATES = {states.QUEUED, states.SENT, states.EXPORTED, states.CLOSED, states.STALE}


def _tab(ctx) -> str:
    return ctx.settings.get("sheet.review_tab", "Review")


def _row_for(con, d) -> list:
    p = db.get_person(con, d["person_id"])
    ev_ids = json.loads(d["evidence_ids"] or "[]")
    urls = []
    for sid in ev_ids:
        r = con.execute("SELECT url FROM signals WHERE signal_id=?", (sid,)).fetchone()
        if r and r["url"]:
            urls.append(r["url"])
    check_notes = ""
    if d["state"] == states.NEEDS_MANUAL:
        res = json.loads(d["checks"] or "{}")
        check_notes = "Write this one by hand (fill the Edited fields, set Edit). Failed: " + \
            "; ".join(f"{k}: {v['reason']}" for k, v in res.items() if not v.get("pass"))
    title_co = " · ".join(x for x in (p["title"], p["primary_employer"]) if x)
    row = [""] * len(REVIEW_COLUMNS)
    row[C["draft_id"]], row[C["person_id"]] = d["draft_id"], d["person_id"]
    row[C["Batch"]], row[C["Group"]] = d["batch_id"], d["segment"]
    row[C["Name"]], row[C["Title and company"]] = p["display_name"] or "", title_co
    row[C["Evidence links"]] = "\n".join(urls)
    row[C["Subject"]], row[C["Body"]] = d["subject"] or "", d["body"] or ""
    row[C["Follow-up 1"]], row[C["Follow-up 2"]] = d["fu1"] or "", d["fu2"] or ""
    row[C["Check notes"]] = check_notes
    row[C["State"]] = states.PUSHED
    return row


def snapshot(ctx) -> tuple[list[list[str]], dict[str, int]]:
    """All rows (header excluded) and draft_id -> 1-based sheet row number."""
    values = ctx.sheets.get_values(_tab(ctx))
    rows = values[1:] if values else []
    index = {}
    for i, r in enumerate(rows):
        did = r[C["draft_id"]] if len(r) > C["draft_id"] else ""
        if did:
            index[did] = i + 2
    return rows, index


def push(ctx, run) -> int:
    con = ctx.con
    _, index = snapshot(ctx)
    todo = con.execute("SELECT * FROM drafts WHERE channel='email' AND state IN (?,?,?) ORDER BY created_at",
                       (states.DRAFTED, states.NEEDS_MANUAL, states.PUSHED)).fetchall()
    new_rows, ids = [], []
    for d in todo:
        if d["state"] == states.PUSHED and d["draft_id"] in index:
            continue  # already on the sheet
        new_rows.append(_row_for(con, d))
        ids.append(d["draft_id"])
    if ctx.dry_run:
        print(f"[dry-run] would add {len(new_rows)} rows to the Review tab")
        return len(new_rows)
    ctx.sheets.append_rows(_tab(ctx), new_rows)
    for did in ids:
        con.execute("UPDATE drafts SET state=?, updated_at=? WHERE draft_id=? AND state IN (?,?,?)",
                    (states.PUSHED, db.now(), did, states.DRAFTED, states.NEEDS_MANUAL, states.PUSHED))
    con.commit()
    run.ok += len(ids)
    return len(ids)


def _cell(row: list, name: str) -> str:
    i = C[name]
    return row[i].strip() if i < len(row) and isinstance(row[i], str) else (str(row[i]) if i < len(row) else "")


def _recheck(ctx, d, edits: dict) -> list[str]:
    person = db.get_person(ctx.con, d["person_id"])
    merged = {k: (edits.get(f"edited_{k}") or d[k] or "") for k in ("subject", "body", "fu1", "fu2")}
    booking = ctx.settings.get(f"sender.booking_links.{d['segment']}", "") or ""
    cctx = check_context(ctx.settings, person, evidence_pack(ctx.con, person), booking)
    res = checks.run_all(merged["subject"], merged["body"], merged["fu1"], merged["fu2"], cctx,
                         footer_text(ctx.settings))
    return checks.failures(res)


def pull(ctx, run) -> dict:
    con = ctx.con
    rows, index = snapshot(ctx)
    updates: list[tuple[str, list[list]]] = []
    counts = {"approved": 0, "edited": 0, "rejected": 0, "failed_recheck": 0, "ignored_locked": 0}
    tab = _tab(ctx)
    state_col, notes_col = col_letter(C["State"]), col_letter(C["Notes"])
    for row in rows:
        did = _cell(row, "draft_id")
        if not did:
            continue
        d = con.execute("SELECT * FROM drafts WHERE draft_id=?", (did,)).fetchone()
        if not d:
            continue
        rownum = index[did]
        edits = {f: _cell(row, label) for f, label in zip(
            states.EDIT_FIELDS, ("Edited subject", "Edited body", "Edited follow-up 1", "Edited follow-up 2"))}
        decision = _cell(row, "Decision")
        if d["state"] in LOCKED_STATES:
            changed = any(edits[f] and edits[f] != (d[f] or "") for f in states.EDIT_FIELDS)
            if changed:
                counts["ignored_locked"] += 1
                updates.append((f"{tab}!{notes_col}{rownum}",
                                [["Edits after queueing are ignored; this email is already queued or sent."]]))
            if _cell(row, "State") != d["state"]:
                updates.append((f"{tab}!{state_col}{rownum}", [[d["state"]]]))
            continue
        if d["state"] != states.PUSHED:
            continue
        new = states.decide(decision, edits)
        if new is None:
            continue
        hand_written = d["subject"] is None  # was needs_manual: there is no generated text to approve
        if hand_written and new != states.REJECTED and not all(edits[f] for f in states.EDIT_FIELDS):
            updates.append((f"{tab}!{notes_col}{rownum}",
                            [["This draft has no generated text. Write the subject, body and both follow-ups "
                              "in the Edited fields and set Edit."]]))
            counts["failed_recheck"] += 1
            continue
        if hand_written and new == states.APPROVED:
            new = states.EDITED
        if new == states.EDITED:
            problems = _recheck(ctx, d, edits)
            if problems:
                counts["failed_recheck"] += 1
                updates.append((f"{tab}!{notes_col}{rownum}", [["Edit failed checks: " + "; ".join(problems)]]))
                continue
        if ctx.dry_run:
            counts[new] += 1
            continue
        reviewer = _cell(row, "Reviewer") or None
        notes = _cell(row, "Notes") or None
        # re-check the state inside the write so a concurrent change is never overwritten
        cur = con.execute(
            "UPDATE drafts SET state=?, edited_subject=?, edited_body=?, edited_fu1=?, edited_fu2=?, "
            "reviewer=?, notes=?, decided_at=?, updated_at=? WHERE draft_id=? AND state=?",
            (new, edits["edited_subject"] or None, edits["edited_body"] or None, edits["edited_fu1"] or None,
             edits["edited_fu2"] or None, reviewer, notes, db.now(), db.now(), did, states.PUSHED))
        if cur.rowcount:
            counts[new] += 1
            updates.append((f"{tab}!{state_col}{rownum}", [[new]]))
    if ctx.dry_run:
        print(f"[dry-run] review decisions that would apply: {counts}")
        return counts
    con.commit()
    ctx.sheets.batch_update_values(updates)
    run.ok += counts["approved"] + counts["edited"] + counts["rejected"]
    run.skipped += counts["failed_recheck"]
    return counts


def protect_locked_rows(ctx) -> int:
    """Add a protected range to rows whose draft is queued or later (once per draft)."""
    con = ctx.con
    _, index = snapshot(ctx)
    tab = _tab(ctx)
    sheet_id = ctx.sheets.tab_ids()[tab]
    reqs = []
    done = []
    for did, rownum in index.items():
        d = con.execute("SELECT state FROM drafts WHERE draft_id=?", (did,)).fetchone()
        if not d or d["state"] not in LOCKED_STATES:
            continue
        if db.get_cursor(con, "sheet_protect", did):
            continue
        reqs.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": sheet_id, "startRowIndex": rownum - 1, "endRowIndex": rownum},
            "description": f"queued draft {did}", "warningOnly": True}}})
        done.append(did)
    if reqs and not ctx.dry_run:
        ctx.sheets.batch_update(reqs)
        for did in done:
            db.set_cursor(con, "sheet_protect", did, "1")
        con.commit()
    return len(done)
