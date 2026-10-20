"""LinkedIn list for engineers with no usable personal email (docs/DESIGN.md section 5).

The pipeline prepares a short connection note; Karishma sends it by hand from her own account.
Nothing here automates LinkedIn.
"""
from __future__ import annotations

import datetime as dt
import json

from .. import db, suppression
from . import checks
from .drafter import check_context, evidence_pack, first_name
from .gemini import GeminiError

MAX_NOTE_CHARS = 280

SYSTEM = """You write a LinkedIn connection note for {company}, which offers consultations for people
exploring the O-1, EB-1A and EB-2 NIW routes in the US. It is sent by {sender} by hand.
Rules: under 250 characters; reference one fact from the EVIDENCE exactly; no link; no visa claims
about the person; never mention nationality, citizenship or visa status; no flattery or hype words.
Return JSON with "note" and "evidence_ids"."""

SCHEMA = {"type": "OBJECT", "properties": {"note": {"type": "STRING"},
                                           "evidence_ids": {"type": "ARRAY", "items": {"type": "STRING"}}},
          "required": ["note", "evidence_ids"]}


def eligible(con, limit: int):
    rows = con.execute(
        "SELECT p.* FROM persons p WHERE p.segment='engineer' AND p.us_located=1 AND p.email_status='none' "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.person_id=p.person_id AND d.channel='linkedin') "
        "AND EXISTS (SELECT 1 FROM person_identifiers i WHERE i.person_id=p.person_id AND i.id_type='linkedin') "
        "LIMIT ?", (limit,)).fetchall()
    return [r for r in rows if not suppression.is_suppressed(con, r["email"])]


def note_checks(note: str, cctx: checks.CheckContext) -> dict:
    res = {"grounded": checks.check_grounded([note], cctx), "no_eligibility": checks.check_no_eligibility([note]),
           "no_sensitive": checks.check_no_sensitive([note]), "tone": checks.check_tone([note])}
    links = len(checks.URL_RE.findall(note))
    res["form"] = (len(note) <= MAX_NOTE_CHARS and links == 0,
                   "" if len(note) <= MAX_NOTE_CHARS and links == 0 else "too long or has a link")
    return {k: {"pass": ok, "reason": why} for k, (ok, why) in res.items()}


def prepare(ctx, run, limit: int) -> int:
    con, s = ctx.con, ctx.settings
    sender, company = s.get("sender.name", ""), s.get("sender.company", "Indoko")
    batch = f"li_{dt.date.today().isoformat()}"
    made = 0
    for p in eligible(con, limit):
        ev = evidence_pack(con, p)
        if not ev:
            run.skipped += 1
            continue
        cctx = check_context(s, p, ev, "")
        prompt = "\n".join([f"RECIPIENT FIRST NAME: {first_name(p['display_name'])}", "EVIDENCE:"] +
                           [f"- [{e['id']}] {e['text']}" for e in ev])
        note, results = "", {}
        for _ in range(2):
            try:
                out = ctx.gemini.generate_json(SYSTEM.format(company=company, sender=sender), prompt, SCHEMA)
            except GeminiError as exc:
                results = {"generation": {"pass": False, "reason": str(exc)}}
                continue
            note = (out.get("note") or "").strip()
            results = note_checks(note, cctx)
            if checks.passed(results):
                break
        state = "drafted" if results and checks.passed(results) else "needs_manual"
        con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,channel,body,checks,state,evidence_ids,"
                    "model,created_at,updated_at) VALUES(?,?,?,?, 'linkedin', ?,?,?,?,?,?,?)",
                    (db.new_id("li"), p["person_id"], batch, "engineer", note if state == "drafted" else None,
                     json.dumps(results), state, json.dumps([e["id"] for e in ev]), s.get("gemini.model"),
                     db.now(), db.now()))
        made += 1
        run.ok += 1
    con.commit()
    return made


def push(ctx, run) -> int:
    """Add prepared notes to the LinkedIn tab."""
    con = ctx.con
    tab = ctx.settings.get("sheet.linkedin_tab", "LinkedIn")
    existing = {r[0] for r in ctx.sheets.get_values(tab)[1:] if r}
    rows = []
    for d in con.execute("SELECT * FROM drafts WHERE channel='linkedin' AND state IN ('drafted','needs_manual')"):
        if d["person_id"] in existing:
            continue
        p = db.get_person(con, d["person_id"])
        li = con.execute("SELECT id_value FROM person_identifiers WHERE person_id=? AND id_type='linkedin'",
                         (d["person_id"],)).fetchone()
        ev = [r["evidence_text"] for r in db.evidence_for(con, d["person_id"])][:2]
        rows.append([d["person_id"], p["display_name"] or "",
                     " · ".join(x for x in (p["title"], p["primary_employer"]) if x),
                     f"https://{li['id_value']}" if li else "", " | ".join(ev),
                     d["body"] or "(write a short note by hand)", "", "", ""])
        con.execute("UPDATE drafts SET state='pushed', updated_at=? WHERE draft_id=?", (db.now(), d["draft_id"]))
    if ctx.dry_run:
        print(f"[dry-run] would add {len(rows)} rows to the LinkedIn tab")
        return len(rows)
    ctx.sheets.append_rows(tab, rows)
    con.commit()
    run.ok += len(rows)
    return len(rows)
