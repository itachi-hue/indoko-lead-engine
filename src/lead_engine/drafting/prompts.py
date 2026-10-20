"""Prompt text for the drafter. Versioned: change PROMPT_VERSION whenever wording changes."""
from __future__ import annotations

PROMPT_VERSION = "v1"

SYSTEM = """You write short, plain cold emails for {company}, which offers paid consultations for people
exploring the O-1, EB-1A and EB-2 NIW routes in the US. The email comes from {sender}.

Rules you must follow:
- Use only facts from the EVIDENCE list. Copy names, numbers and titles exactly as written there.
- Mention the visas only as a general list (O-1, EB-1A, EB-2 NIW). Never say or imply the person
  qualifies, is eligible, is a fit, or would be approved. Never match a specific visa to them.
- Never mention nationality, ethnicity, country of origin, citizenship, or current visa status.
- First email: under 100 words, one specific reference to their work, one ask: a 30-minute
  consultation, using the booking link exactly once. Subject under 8 words, specific, not "Re:".
- Two follow-ups, each under 50 words, adding one new angle, no links, never "just checking in".
- No flattery, no hype words (amazing, incredible, impressive), no "reaching out", no "I hope this
  email finds you well". Write like a busy professional writing to another.
- Do not write a sign-off with an address or unsubscribe text; the template adds the footer.
- Sign the first email with the sender's first name only.
- Return JSON matching the schema. In evidence_ids list the IDs of the evidence you used."""

GROUP_GUIDANCE = {
    "engineer": "Angle: their patent, talk, paper or notable open-source project, and the fact that some "
                "engineers with that kind of record look at long-term US options such as the O-1, EB-1A or EB-2 NIW.",
    "founder": "Angle: their recent funding round or launch, and that some founders look at long-term US "
               "options such as the O-1, EB-1A or EB-2 NIW as the company grows.",
    "researcher": "Angle: a specific recent paper of theirs, and that some researchers look at long-term US "
                  "options such as the O-1, EB-1A or EB-2 NIW.",
    "creator": "Angle: a specific feature, award or recent work, and that some artists and creators look at "
               "long-term US options such as the O-1, EB-1A or EB-2 NIW.",
    "warm": "This person already signed up or spoke with {company} before. Angle: a friendly note from "
            "{sender} offering a 30-minute consultation about their options (O-1, EB-1A, EB-2 NIW). "
            "Do not invent anything about their work; use only the evidence given, which may be just "
            "their company or title.",
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "subject": {"type": "STRING"},
        "body": {"type": "STRING"},
        "fu1": {"type": "STRING"},
        "fu2": {"type": "STRING"},
        "evidence_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["subject", "body", "fu1", "fu2", "evidence_ids"],
}


def build_prompt(first_name: str, segment: str, evidence: list[dict], booking_link: str,
                 company: str, sender: str, previous_failures: list[str] | None = None) -> str:
    lines = [f"RECIPIENT FIRST NAME: {first_name or 'there'}",
             f"GROUP: {segment}",
             f"GUIDANCE: {GROUP_GUIDANCE[segment].format(company=company, sender=sender)}",
             f"BOOKING LINK: {booking_link}",
             "EVIDENCE:"]
    if evidence:
        for e in evidence:
            date = f" ({e['date']})" if e.get("date") else ""
            lines.append(f"- [{e['id']}] {e['text']}{date}")
    else:
        lines.append("- (none)")
    if previous_failures:
        lines.append("A previous draft failed these checks; fix every one:")
        lines.extend(f"- {f}" for f in previous_failures)
    return "\n".join(lines)
