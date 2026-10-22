"""Turns eligible people into drafts: brief -> Gemini -> checks -> regenerate once -> needs_manual."""
from __future__ import annotations

import datetime as dt
import json
import logging

from .. import db, suppression
from ..states import ACTIVE_STATES, DRAFTED, NEEDS_MANUAL
from . import checks, prompts
from .gemini import GeminiError

log = logging.getLogger("lead_engine.drafter")
MAX_EVIDENCE = 3


def footer_text(settings) -> str:
    s = settings.section("sender")
    name, company, address = s.get("name", ""), s.get("company", "Indoko"), s.get("postal_address", "")
    if not (name and address):
        return ""
    return (f"{name}, {company} · {address}\n"
            f"This is a message about {company}'s services. "
            f"Reply \"unsubscribe\" and we won't email you again.")


def first_name(display_name: str | None) -> str:
    return (display_name or "").split()[0] if display_name else ""


def eligible_people(con, segment: str, limit: int, us_only: bool = True, require_moved: bool = False):
    """People in a group with a usable email, no active draft, never contacted, not suppressed.
    require_moved: cold groups only draft people who passed the moved-to-the-US filter."""
    rows = con.execute(
        f"""SELECT p.* FROM persons p
            LEFT JOIN scores s ON s.person_id = p.person_id
            WHERE p.segment = ? AND p.email IS NOT NULL
              AND p.email_status IN ('verified', 'catch_all_published', 'on_file')
              AND EXISTS (SELECT 1 FROM person_identifiers i WHERE i.person_id = p.person_id
                          AND i.id_type = 'email' AND i.id_value = lower(p.email))
              AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.person_id = p.person_id
                              AND d.state IN ({','.join('?' * len(ACTIVE_STATES))}))
              AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.person_id = p.person_id
                              AND d.state IN ('closed','rejected'))
              AND NOT EXISTS (SELECT 1 FROM events e WHERE e.person_id = p.person_id)
            GROUP BY p.person_id
            ORDER BY MAX(COALESCE(s.percentile, 0)) DESC, p.created_at
         """, (segment, *ACTIVE_STATES)).fetchall()
    out = []
    for r in rows:
        if us_only and r["us_located"] != 1:
            continue
        if suppression.is_suppressed(con, r["email"]):
            continue
        if require_moved and segment != "warm" and not con.execute(
                "SELECT 1 FROM signals WHERE person_id=? AND signal_type='moved_to_us'", (r["person_id"],)).fetchone():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def evidence_pack(con, person) -> list[dict]:
    items = [{"id": r["signal_id"], "text": r["evidence_text"], "date": r["evidence_date"], "url": r["url"]}
             for r in db.evidence_for(con, person["person_id"])][:MAX_EVIDENCE]
    if person["segment"] == "warm":  # warm contacts: only what's on file
        on_file = []
        if person["title"]:
            on_file.append(f"Title on file: {person['title']}")
        if person["primary_employer"]:
            on_file.append(f"Company on file: {person['primary_employer']}")
        if on_file:
            items.append({"id": f"file:{person['person_id']}", "text": ". ".join(on_file), "date": None, "url": None})
    return items


def check_context(settings, person, evidence: list[dict], booking_link: str) -> checks.CheckContext:
    sender = settings.get("sender.name", "")
    company = settings.get("sender.company", "Indoko")
    allow = [person["display_name"] or "", first_name(person["display_name"]), sender, first_name(sender),
             company, booking_link, "30-minute 30 minutes"]
    return checks.CheckContext(evidence_texts=[e["text"] for e in evidence if e.get("text")], allowlist=allow)


def draft_one(ctx, person, batch_id: str) -> tuple[str, str]:
    """Create one draft. Returns (draft_id, state)."""
    settings, con = ctx.settings, ctx.con
    segment = person["segment"]
    booking = settings.get(f"sender.booking_links.{segment}", "") or ""
    sender = settings.get("sender.name", "")
    company = settings.get("sender.company", "Indoko")
    evidence = evidence_pack(con, person)
    cctx = check_context(settings, person, evidence, booking)
    footer = footer_text(settings)
    attempts = 1 + int(settings.get("gemini.max_regenerations", 1))
    fail_reasons: list[str] = []
    out: dict = {}
    results: dict = {}
    for _ in range(attempts):
        prompt = prompts.build_prompt(first_name(person["display_name"]), segment,
                                      [{"id": e["id"], "text": e["text"], "date": e["date"]} for e in evidence],
                                      booking, company, sender, fail_reasons or None)
        try:
            out = ctx.gemini.generate_json(prompts.SYSTEM.format(company=company, sender=sender), prompt,
                                           prompts.RESPONSE_SCHEMA)
        except GeminiError as exc:
            fail_reasons = [f"generation: {exc}"]
            results = {"generation": {"pass": False, "reason": str(exc)}}
            continue
        valid_ids = {e["id"] for e in evidence}
        cited = [i for i in out.get("evidence_ids", []) if i in valid_ids]
        cited_ctx = checks.CheckContext(
            evidence_texts=[e["text"] for e in evidence if e["id"] in cited] or cctx.evidence_texts,
            allowlist=cctx.allowlist)
        results = checks.run_all(out.get("subject", ""), out.get("body", ""), out.get("fu1", ""),
                                 out.get("fu2", ""), cited_ctx, footer)
        if checks.passed(results):
            out["evidence_ids"] = cited
            break
        fail_reasons = checks.failures(results)
    ok = bool(results) and checks.passed(results)
    state = DRAFTED if ok else NEEDS_MANUAL
    draft_id = db.new_id("d")
    con.execute(
        "INSERT INTO drafts(draft_id,person_id,batch_id,segment,channel,subject,body,fu1,fu2,evidence_ids,"
        "model,prompt_version,checks,state,created_at,updated_at) VALUES(?,?,?,?, 'email', ?,?,?,?,?,?,?,?,?,?,?)",
        (draft_id, person["person_id"], batch_id, segment,
         out.get("subject") if ok else None, out.get("body") if ok else None,
         out.get("fu1") if ok else None, out.get("fu2") if ok else None,
         json.dumps(out.get("evidence_ids", [])), settings.get("gemini.model"), prompts.PROMPT_VERSION,
         json.dumps(results), state, db.now(), db.now()))
    con.commit()
    return draft_id, state


def draft_batch(ctx, run, segment: str, limit: int) -> str:
    us_only = bool(ctx.settings.get("pilot.us_only", True))
    batch_id = f"b_{dt.date.today().isoformat()}_{segment}"
    people = eligible_people(ctx.con, segment, limit, us_only,
                             require_moved=bool(ctx.settings.get("pilot.draft_only_movers", False)))
    if not footer_text(ctx.settings):
        raise RuntimeError("sender.name and sender.postal_address must be set before drafting")
    for person in people:
        try:
            _, state = draft_one(ctx, person, batch_id)
        except Exception as exc:  # one person failing must not stop the batch
            run.failed += 1
            log.warning("draft failed for %s: %s", person["person_id"], exc)
            continue
        if state == DRAFTED:
            run.ok += 1
        else:
            run.skipped += 1
    return batch_id
