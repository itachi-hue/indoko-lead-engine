"""`lead-engine forget <email>`: delete a person everywhere and keep only a hashed suppression entry
(docs/DESIGN.md section 10)."""
from __future__ import annotations

from . import db, http, suppression


def forget(ctx, run, email: str) -> dict:
    con = ctx.con
    e = suppression.normalize_email(email) or email.strip().lower()
    pid = db.find_person_by_identifier(con, "email", e)
    if not pid:
        r = con.execute("SELECT person_id FROM persons WHERE lower(email)=?", (e,)).fetchone()
        pid = r["person_id"] if r else None
    leads = []
    if pid:
        leads = con.execute("SELECT s.instantly_lead_id, s.method FROM sends s JOIN drafts d "
                            "ON d.draft_id=s.draft_id WHERE d.person_id=?", (pid,)).fetchall()
    result = {"found": bool(pid), "instantly_leads_deleted": 0, "sheet_rows_cleared": 0}
    if ctx.dry_run:
        print(f"[dry-run] would forget {'1 person' if pid else 'no stored person'} and suppress the hash")
        return result

    # 3. hashed suppression entry first, so the address can never be re-imported even if a step fails
    suppression.add(con, "email_hash", suppression.email_hash(e), "forget request", "forget")
    con.execute("DELETE FROM suppression WHERE match_type='email' AND value=?", (e,))
    con.commit()

    # 4. Instantly: delete leads, add to blocklist (CSV mode: export for manual blocklisting)
    from .instantly.events import write_suppression_export

    if ctx.settings.get("instantly.mode", "auto") != "csv" and ctx.settings.secret("INSTANTLY_API_KEY"):
        for lead in leads:
            if lead["instantly_lead_id"]:
                try:
                    ctx.instantly.delete_lead(lead["instantly_lead_id"])
                    result["instantly_leads_deleted"] += 1
                except http.HTTPError as exc:
                    run.note(f"Instantly lead delete failed (HTTP {exc.status}); delete it by hand")
        try:
            ctx.instantly.block(e)
        except http.HTTPError as exc:
            run.note(f"Instantly blocklist add failed (HTTP {exc.status}); listed in suppression-export.csv")
            write_suppression_export(ctx, [e])
    else:
        write_suppression_export(ctx, [e])

    # 2. Sheet rows (the Suppression tab too, so the plain address isn't re-imported)
    if ctx.settings.get("sheet.spreadsheet_id"):
        stab = ctx.settings.get("sheet.suppression_tab", "Suppression")
        for i, r in enumerate(ctx.sheets.get_values(stab)[1:], start=2):
            if r and (r[0] or "").strip().lower() == e:
                ctx.sheets.update_values(f"{stab}!A{i}:D{i}", [["", "", "", ""]])
                result["sheet_rows_cleared"] += 1
    if pid and ctx.settings.get("sheet.spreadsheet_id"):
        from .sheets import crm, review

        result["sheet_rows_cleared"] += crm.remove_person_rows(ctx, pid)
        rows, index = review.snapshot(ctx)
        for r in rows:
            if len(r) > review.C["person_id"] and r[review.C["person_id"]] == pid:
                n = index.get(r[review.C["draft_id"]])
                if n:
                    width = len(review.REVIEW_COLUMNS)
                    from .sheets.layout import col_letter
                    ctx.sheets.update_values(f"{review._tab(ctx)}!A{n}:{col_letter(width - 1)}{n}", [[""] * width])
                    result["sheet_rows_cleared"] += 1

    # 1. database rows (cascades to identifiers, signals, scores, drafts, sends, events)
    if pid:
        with con:
            con.execute("DELETE FROM enrichment_ledger WHERE person_id=?", (pid,))
            con.execute("DELETE FROM identity_edges WHERE a_val=? OR b_val=?", (e, e))
            con.execute("DELETE FROM source_records WHERE lower(source_native_id)=?", (e,))
            con.execute("DELETE FROM crawl_cursors WHERE ckey=?", (pid,))
            con.execute("DELETE FROM persons WHERE person_id=?", (pid,))
    run.ok += 1
    return result
