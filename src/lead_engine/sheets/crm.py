"""CRM and Suppression tab sync (docs/DESIGN.md section 5).

push writes only pipeline-owned columns; it never touches team-owned columns.
pull reads team-owned columns (booked, attended, one-pager sent, client, reply reason) into
`events`, and new Suppression-tab rows into the suppression list.
"""
from __future__ import annotations

import logging

from .. import db, states, suppression
from .layout import CRM_COLUMNS, CRM_PIPELINE_COLUMNS, SUPPRESSION_COLUMNS, col_letter

log = logging.getLogger("lead_engine.crm")
C = {name: i for i, name in enumerate(CRM_COLUMNS)}
TEAM_EVENT_COLUMNS = {"Booked": "booked", "Attended": "attended", "One-pager sent": "onepager_sent",
                      "Client": "client"}
NO_VALUES = {"", "no", "n", "false", "0", "-"}


def _tab(ctx, key: str, default: str) -> str:
    return ctx.settings.get(f"sheet.{key}", default)


def contacted_people(con):
    return con.execute("SELECT DISTINCT p.* FROM persons p WHERE EXISTS "
                       "(SELECT 1 FROM sends s JOIN drafts d ON d.draft_id=s.draft_id "
                       " WHERE d.person_id=p.person_id AND s.status IN ('sent','exported'))").fetchall()


def pipeline_row(con, p, flags: dict[str, str]) -> list[str]:
    ev = con.execute("SELECT type, occurred_at FROM events WHERE person_id=? ORDER BY occurred_at",
                     (p["person_id"],)).fetchall()
    first_sent = con.execute("SELECT MIN(s.sent_at) t FROM sends s JOIN drafts d ON d.draft_id=s.draft_id "
                             "WHERE d.person_id=?", (p["person_id"],)).fetchone()["t"] or ""
    pos = next((e["occurred_at"] for e in ev if e["type"] == "positive_reply"), "")
    last = f"{ev[-1]['type']} {ev[-1]['occurred_at'][:10]}" if ev else ""
    return [p["person_id"], p["display_name"] or "", p["segment"] or "", p["group_source"] or "",
            p["email"] or "", states.lead_status([e["type"] for e in ev]), first_sent[:10], last,
            pos[:10], flags.get(p["person_id"], "")]


def push(ctx, run, flags: dict[str, str] | None = None) -> dict:
    con = ctx.con
    tab = _tab(ctx, "crm_tab", "CRM")
    values = ctx.sheets.get_values(tab)
    existing = {}
    for i, r in enumerate(values[1:] if values else []):
        if r and r[0]:
            existing[r[0]] = i + 2
    updates, appends = [], []
    last_col = col_letter(len(CRM_PIPELINE_COLUMNS) - 1)
    for p in contacted_people(con):
        row = pipeline_row(con, p, flags or {})
        if p["person_id"] in existing:
            n = existing[p["person_id"]]
            updates.append((f"{tab}!A{n}:{last_col}{n}", [row]))
        else:
            appends.append(row + [""] * (len(CRM_COLUMNS) - len(row)))
    if ctx.dry_run:
        print(f"[dry-run] CRM: would update {len(updates)} rows and add {len(appends)}")
        return {"updated": len(updates), "added": len(appends)}
    ctx.sheets.batch_update_values(updates)
    ctx.sheets.append_rows(tab, appends)
    run.ok += len(updates) + len(appends)
    return {"updated": len(updates), "added": len(appends)}


def pull(ctx, run) -> dict:
    con = ctx.con
    tab = _tab(ctx, "crm_tab", "CRM")
    values = ctx.sheets.get_values(tab)
    added = 0
    header = values[0] if values else CRM_COLUMNS
    idx = {name: header.index(name) for name in CRM_COLUMNS if name in header}
    for r in values[1:] if values else []:
        pid = r[0] if r else ""
        if not pid or not db.get_person(con, pid):
            continue
        for col, etype in TEAM_EVENT_COLUMNS.items():
            i = idx.get(col)
            val = (r[i] if i is not None and i < len(r) else "").strip()
            if val.lower() in NO_VALUES:
                continue
            occurred = val if len(val) >= 10 and val[4] == "-" else None
            if db.add_event(con, pid, etype, "crm", occurred_at=(occurred + "T00:00:00Z") if occurred else None,
                            external_id=f"crm:{pid}:{etype}"):
                added += 1
        i = idx.get("Reply reason")
        reason = (r[i] if i is not None and i < len(r) else "").strip().lower()
        if reason:
            db.set_cursor(con, "reply_reason", pid, reason)
    con.commit()
    run.ok += added
    return {"events_added": added, "suppression_added": pull_suppression(ctx)}


def pull_suppression(ctx) -> int:
    con = ctx.con
    tab = _tab(ctx, "suppression_tab", "Suppression")
    values = ctx.sheets.get_values(tab)
    added = 0
    for r in values[1:] if values else []:
        val = (r[0] if r else "").strip().lower()
        if not val:
            continue
        reason = r[1] if len(r) > 1 else "manual"
        if "@" in val:
            e = suppression.normalize_email(val)
            if e and suppression.add(con, "email", e, reason or "manual", "manual"):
                added += 1
        elif "." in val:
            if suppression.add(con, "domain", val.lstrip("@"), reason or "manual", "manual"):
                added += 1
    con.commit()
    return added


def remove_person_rows(ctx, person_id: str) -> int:
    """Blank out a person's rows in the CRM and LinkedIn tabs (used by `forget`)."""
    cleared = 0
    for key, default in (("crm_tab", "CRM"), ("linkedin_tab", "LinkedIn")):
        tab = _tab(ctx, key, default)
        values = ctx.sheets.get_values(tab)
        for i, r in enumerate(values[1:] if values else []):
            if r and r[0] == person_id:
                width = max(len(values[0]), len(r))
                ctx.sheets.update_values(f"{tab}!A{i + 2}:{col_letter(width - 1)}{i + 2}", [[""] * width])
                cleared += 1
    return cleared


SUPPRESSION_HEADER = SUPPRESSION_COLUMNS
