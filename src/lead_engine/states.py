"""Draft state machine and lead status (docs/DESIGN.md section 5)."""
from __future__ import annotations

DRAFTED, PUSHED, APPROVED, EDITED, REJECTED = "drafted", "pushed", "approved", "edited", "rejected"
QUEUED, SENT, EXPORTED, CLOSED = "queued", "sent", "exported", "closed"
NEEDS_MANUAL, STALE = "needs_manual", "stale"

ACTIVE_STATES = {DRAFTED, NEEDS_MANUAL, PUSHED, APPROVED, EDITED, QUEUED, SENT, EXPORTED}
SENDABLE = {APPROVED, EDITED}

TRANSITIONS = {
    DRAFTED: {PUSHED, STALE},
    NEEDS_MANUAL: {PUSHED, STALE},
    PUSHED: {APPROVED, EDITED, REJECTED, STALE, PUSHED},
    APPROVED: {QUEUED, STALE},
    EDITED: {QUEUED, STALE, PUSHED},
    QUEUED: {SENT, EXPORTED, STALE},
    SENT: {CLOSED},
    EXPORTED: {SENT, CLOSED},
    REJECTED: set(),
    STALE: set(),
    CLOSED: set(),
}

EDIT_FIELDS = ("edited_subject", "edited_body", "edited_fu1", "edited_fu2")


class InvalidTransition(Exception):
    pass


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"{current} -> {new}")


def decide(decision: str | None, edits: dict[str, str | None]) -> str | None:
    """Map a reviewer's row to a draft state. None means no decision yet.

    Edit, or any edited field with text, means `edited`. Approve with all edited fields empty
    means `approved`. Reject is final.
    """
    d = (decision or "").strip().lower()
    has_edits = any((edits.get(f) or "").strip() for f in EDIT_FIELDS)
    if d == "reject":
        return REJECTED
    if d == "edit" or (d == "approve" and has_edits):
        return EDITED
    if d == "approve":
        return APPROVED
    return None


def final_text(draft) -> dict[str, str]:
    """The text that is actually sent: reviewer edits win field by field."""
    def pick(edited, original):
        e = draft[edited]
        return e if e and e.strip() else (draft[original] or "")
    return {"subject": pick("edited_subject", "subject"), "body": pick("edited_body", "body"),
            "fu1": pick("edited_fu1", "fu1"), "fu2": pick("edited_fu2", "fu2")}


# ---------------- lead status (derived from events, never stored) ----------------
PROGRESS = ["sent", "reply", "positive_reply", "booked", "attended", "client"]
CLOSERS = {"unsubscribe": "unsubscribed", "bounce": "bounced", "not_interested": "not interested"}


def lead_status(event_types: list[str]) -> str:
    """client > attended > booked > positive_reply > reply > sent; closers override."""
    types = set(event_types)
    for t, label in CLOSERS.items():
        if t in types:
            return label
    best = None
    for t in PROGRESS:
        if t in types:
            best = t
    if "onepager_sent" in types and best in (None, "sent", "reply", "positive_reply", "booked", "attended"):
        # one-pager follows an attended consultation; it doesn't outrank client
        best = "onepager_sent" if best != "client" else best
    labels = {"sent": "emailed", "reply": "replied", "positive_reply": "positive reply",
              "booked": "booked", "attended": "attended", "onepager_sent": "one-pager sent",
              "client": "client"}
    return labels.get(best, "not contacted")


def person_status(con, person_id: str) -> str:
    rows = con.execute("SELECT type FROM events WHERE person_id=?", (person_id,)).fetchall()
    return lead_status([r["type"] for r in rows])
