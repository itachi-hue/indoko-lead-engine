"""The six automated checks every draft passes before review (docs/DESIGN.md section 7).

Each check returns (passed, reason). They are deliberately conservative: a false alarm costs a
regeneration or a human rewrite; a miss can put a wrong or risky email in front of a person.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VISA_NAMES = re.compile(r"\b(?:O-1A?|O-1B|EB-1A?|EB-2(?:\s*NIW)?|NIW)\b", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
NUMBER_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]\b|million\b|billion\b|thousand\b)?")
CAP_WORD = r"(?:[A-Z][a-zA-Z0-9&'\-.]*|[A-Z]{2,})"
CAP_PHRASE_RE = re.compile(rf"\b{CAP_WORD}(?:\s+(?:of|the|and|for|de|&)?\s*{CAP_WORD})+")

# Words that are capitalized for grammar, not because they name something.
LEADING_COMMON = {
    "hi", "hello", "hey", "dear", "congratulations", "congrats", "i", "i'm", "i've", "i'd", "if", "the",
    "your", "you", "would", "thanks", "thank", "best", "saw", "we", "our", "this", "that", "when",
    "after", "since", "a", "an", "it", "its", "it's", "as", "in", "on", "at", "for", "with", "and",
    "but", "so", "or", "no", "not", "are", "is", "was", "were", "do", "does", "did", "can", "could",
    "should", "will", "just", "also", "here", "there", "what", "how", "why", "who", "my", "me",
    "regards", "cheers", "warm", "looking", "happy", "glad", "quick", "following", "reading", "noticed",
    "many", "some", "most", "one", "two", "great", "nice", "loved", "enjoyed", "following",
}

ELIGIBILITY_RE = re.compile(
    r"\b(qualif(?:y|ies|ied|ication)|eligib(?:le|ility)|guarantee[sd]?|approved|approval|"
    r"strong case|sure to (?:get|win)|will get (?:your|a) (?:visa|green card)|"
    r"you(?:'d| would| could| will) (?:likely )?(?:get|obtain|be granted|be approved))\b", re.I)
VISA_MATCH_RE = re.compile(
    r"\b(?:you(?:'re| are)?|your profile|your record|your work)\b[^.?!]{0,60}\b(?:fit|match|perfect|ideal|"
    r"candidate)\b[^.?!]{0,40}" + VISA_NAMES.pattern, re.I)

SENSITIVE_RE = re.compile(
    r"\b(nationality|citizenship|ethnic(?:ity)?|racial|home country|country of (?:origin|birth)|"
    r"immigrants?|foreign nationals?|foreigners?|international (?:students?|workers?|talent)|"
    r"visa status|h-?1b|f-?1 (?:visa|student)|stem opt|opt extension|green card holder|"
    r"your current visa|work permit|deport\w*|undocumented)\b", re.I)
DEMONYMS = {
    "indian", "chinese", "korean", "japanese", "vietnamese", "filipino", "pakistani", "bangladeshi",
    "nigerian", "ghanaian", "kenyan", "brazilian", "mexican", "colombian", "argentinian", "russian",
    "ukrainian", "iranian", "israeli", "turkish", "egyptian", "german", "french", "italian", "spanish",
    "british", "irish", "canadian", "australian", "african", "asian", "latino", "latina", "latinx",
    "hispanic", "european", "arab", "persian", "taiwanese", "singaporean", "sri lankan", "nepali",
}

BANNED_PHRASES = [
    "hope this email finds you", "hope you're doing well", "hope you are doing well", "hope you're well",
    "i came across your profile", "came across your profile", "reaching out", "touch base",
    "circle back", "synergy", "game-changer", "game changer", "cutting-edge", "world-class",
    "groundbreaking", "revolutionary", "just checking in", "quick question", "pick your brain",
    "no pitch", "not a pitch", "amazing", "incredible", "impressive", "thrilled", "excited to",
    "i'm excited", "delve", "unlock your", "take your career", "dream", "journey", "leverage",
]

MAX_BODY_WORDS = 120
MAX_FOLLOWUP_WORDS = 60
MAX_SUBJECT_WORDS = 8


@dataclass
class CheckContext:
    evidence_texts: list[str]
    allowlist: list[str]          # names, sender, company, booking link, template numbers


def _norm_number(tok: str) -> float | None:
    t = tok.strip().lower().replace(",", "").replace("$", "").strip()
    mult = 1.0
    for suffix, m in (("billion", 1e9), ("million", 1e6), ("thousand", 1e3), ("b", 1e9), ("m", 1e6),
                      ("k", 1e3), ("%", 1.0)):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
            mult = m
            break
    try:
        return round(float(t) * mult, 4)
    except ValueError:
        return None


def numbers_in(text: str) -> set[float]:
    text = VISA_NAMES.sub(" ", text)
    text = URL_RE.sub(" ", text)
    out = set()
    for m in NUMBER_RE.finditer(text):
        n = _norm_number(m.group(0))
        if n is not None:
            out.add(n)
    return out


def phrases_in(text: str) -> list[str]:
    text = VISA_NAMES.sub(" ", text)
    text = URL_RE.sub(" ", text)
    found = []
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        for m in CAP_PHRASE_RE.finditer(sentence):
            words = m.group(0).split()
            while words and words[0].lower().strip("',.") in LEADING_COMMON:
                words = words[1:]
            caps = [w for w in words if w[:1].isupper()]
            if len(caps) >= 2:
                found.append(" ".join(words))
    return found


def _haystack(ctx: CheckContext) -> str:
    return " \n ".join(ctx.evidence_texts + ctx.allowlist).lower()


def check_grounded(texts: list[str], ctx: CheckContext) -> tuple[bool, str]:
    hay = _haystack(ctx)
    allowed_numbers = set()
    for t in ctx.evidence_texts + ctx.allowlist:
        allowed_numbers |= numbers_in(t)
    missing = []
    for text in texts:
        for phrase in phrases_in(text):
            if phrase.lower() not in hay:
                missing.append(phrase)
        for n in numbers_in(text):
            if n not in allowed_numbers:
                missing.append(str(n).rstrip("0").rstrip(".") if "." in str(n) else str(n))
    if missing:
        return False, "not found in evidence: " + ", ".join(sorted(set(missing))[:6])
    return True, ""


def check_no_eligibility(texts: list[str]) -> tuple[bool, str]:
    for t in texts:
        m = ELIGIBILITY_RE.search(t) or VISA_MATCH_RE.search(t)
        if m:
            return False, f"eligibility language: '{m.group(0)[:60]}'"
    return True, ""


def check_no_sensitive(texts: list[str]) -> tuple[bool, str]:
    for t in texts:
        m = SENSITIVE_RE.search(t)
        if m:
            return False, f"sensitive reference: '{m.group(0)}'"
        low = t.lower()
        hits = sorted(d for d in DEMONYMS if re.search(rf"\b{re.escape(d)}\b", low))
        if hits:
            return False, f"nationality or ethnicity word: '{hits[0]}'"
    return True, ""


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text or ""))


def check_form(subject: str, body: str, followups: list[str]) -> tuple[bool, str]:
    problems = []
    if not subject.strip():
        problems.append("empty subject")
    if word_count(subject) > MAX_SUBJECT_WORDS:
        problems.append(f"subject over {MAX_SUBJECT_WORDS} words")
    if re.match(r"^\s*(re|fwd?)\s*:", subject, re.I):
        problems.append("subject pretends to be a reply or forward")
    if word_count(body) > MAX_BODY_WORDS:
        problems.append(f"body over {MAX_BODY_WORDS} words ({word_count(body)})")
    if len(URL_RE.findall(body)) > 1:
        problems.append("more than one link")
    if body.count("?") > 1:
        problems.append("more than one ask")
    for i, fu in enumerate(followups, 1):
        if fu and word_count(fu) > MAX_FOLLOWUP_WORDS:
            problems.append(f"follow-up {i} over {MAX_FOLLOWUP_WORDS} words")
        if fu and len(URL_RE.findall(fu)) > 1:
            problems.append(f"follow-up {i} has more than one link")
    return (not problems), "; ".join(problems)


def check_footer(texts: list[str], footer: str) -> tuple[bool, str]:
    if not footer or "unsubscribe" not in footer.lower():
        return False, "footer is not configured (sender name, postal address, unsubscribe line)"
    for t in texts:
        if "unsubscribe" in t.lower() or "opt out" in t.lower():
            return False, "the footer is added by the template; the email text must not include its own"
    return True, ""


def check_tone(texts: list[str]) -> tuple[bool, str]:
    for t in texts:
        low = t.lower()
        for p in BANNED_PHRASES:
            if re.search(rf"\b{re.escape(p)}\b", low):
                return False, f"banned phrase: '{p}'"
    return True, ""


def run_all(subject: str, body: str, fu1: str, fu2: str, ctx: CheckContext, footer: str) -> dict:
    texts = [subject, body, fu1 or "", fu2 or ""]
    results = {
        "grounded": check_grounded(texts, ctx),
        "no_eligibility": check_no_eligibility(texts),
        "no_sensitive": check_no_sensitive(texts),
        "form": check_form(subject, body, [fu1 or "", fu2 or ""]),
        "footer": check_footer(texts, footer),
        "tone": check_tone(texts),
    }
    return {k: {"pass": ok, "reason": reason} for k, (ok, reason) in results.items()}


def passed(results: dict) -> bool:
    return all(r["pass"] for r in results.values())


def failures(results: dict) -> list[str]:
    return [f"{k}: {r['reason']}" for k, r in results.items() if not r["pass"]]
