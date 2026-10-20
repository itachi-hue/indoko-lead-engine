"""`find-emails`: choose one usable, verified address per person, within a dollar budget."""
from __future__ import annotations

import logging

from .. import db, http, suppression
from . import finders, policy
from . import verify as verify_mod

log = logging.getLogger("lead_engine.find")


def candidates_for(con, segment: str, limit: int):
    return con.execute(
        "SELECT p.* FROM persons p LEFT JOIN scores s ON s.person_id=p.person_id "
        "WHERE p.segment=? AND p.us_located=1 AND p.email IS NULL AND COALESCE(p.email_status,'')<>'none' "
        "GROUP BY p.person_id ORDER BY MAX(COALESCE(s.percentile,0)) DESC LIMIT ?", (segment, limit)).fetchall()


def _verify(con, verifier, email: str, person_id: str, run) -> str:
    rh = db.stable_id("verify", email)
    seen = db.ledger_seen(con, verifier.name, rh)
    if seen:
        return (seen["result"] or {}).get("status", "unknown")
    status = verifier.verify(email)
    db.ledger_add(con, person_id, verifier.name, rh, verifier.cost_usd, "hit", {"status": status})
    run.cost_usd += verifier.cost_usd
    return status


def find_for_person(ctx, person, run, verifier, avail: dict, budget_left) -> str | None:
    con = ctx.con
    order = [p for p in finders.GROUP_ORDER.get(person["segment"], []) if p in avail]
    for name in order:
        if budget_left() <= 0:
            return None
        rh = finders.request_hash(name, person["person_id"])
        if db.ledger_seen(con, name, rh):
            continue  # never pay for the same lookup twice
        finder = avail[name]
        before = run.cost_usd
        try:
            cands = finder.find(ctx, person, run)
        except http.HTTPError as exc:
            db.ledger_add(con, person["person_id"], name, rh, finder.cost_usd, "error", {"status": exc.status})
            run.note(f"{name} lookup failed (HTTP {exc.status})")
            continue
        cost = finder.cost_usd + (run.cost_usd - before)
        run.cost_usd = before + cost
        db.ledger_add(con, person["person_id"], name, rh, cost, "hit" if cands else "miss",
                      {"count": len(cands)})
        for c in cands:
            email = suppression.normalize_email(c["email"])
            if not email or suppression.is_suppressed(con, email):
                continue
            if policy.violates_engineer_rule(person, email) or suppression.is_role_address(email):
                continue
            status = _verify(con, verifier, email, person["person_id"], run)
            ok, why = policy.acceptable(person, email, c["source"], status)
            if not ok:
                log.info("rejected candidate for %s: %s", person["person_id"], why)
                continue
            if db.add_identifier(con, person["person_id"], "email", email, "found") == "conflict":
                continue  # this address belongs to someone else on file
            db.touch_person(con, person["person_id"], email=email, email_source=c["source"],
                            email_status="verified" if status == "deliverable" else "catch_all_published")
            con.commit()
            return email
    return None


def find_emails(ctx, run, segment: str, limit: int, budget_usd: float) -> dict:
    verifier = verify_mod.build(ctx)
    if verifier is None:
        raise RuntimeError("no email verifier configured (emails.verifier + VERIFIER_API_KEY); "
                           "unverified addresses are never used")
    avail = finders.available(ctx)
    start_cost = run.cost_usd
    found = missed = 0
    for person in candidates_for(ctx.con, segment, limit):
        if run.cost_usd - start_cost >= budget_usd:
            run.note(f"budget cap ${budget_usd:.2f} reached")
            break
        email = find_for_person(ctx, person, run, verifier, avail,
                                lambda: budget_usd - (run.cost_usd - start_cost))
        if email:
            found += 1
            run.ok += 1
        else:
            missed += 1
            run.skipped += 1
            tried = [p for p in finders.GROUP_ORDER.get(segment, []) if p in avail]
            if all(db.ledger_seen(ctx.con, p, finders.request_hash(p, person["person_id"])) for p in tried):
                db.touch_person(ctx.con, person["person_id"], email_status="none")
        ctx.con.commit()
    return {"found": found, "missed": missed, "cost_usd": round(run.cost_usd - start_cost, 4)}
