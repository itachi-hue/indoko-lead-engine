"""Company domains and founder email candidates, found for free by fetching pages directly.

A domain is accepted only when its homepage names the company (in the <title>, or in the text
together with a word specific to that company), so a namesake's site isn't used.
"""
from __future__ import annotations

import re

import httpx

STOP = {"labs", "lab", "company", "inc", "llc", "corp", "corporation", "systems", "technologies", "the", "co"}
PATTERNS = ("{f}", "{f}.{l}", "{f}{l}", "{fi}{l}", "{f}{li}", "{f}_{l}")
EXTRA_PAGES = ("", "/about", "/team", "/contact", "/company", "/about-us")


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", name or "")]


def candidate_domains(company: str) -> list[str]:
    words = _words(company)
    core = [w for w in words if w not in STOP] or words
    slugs = list(dict.fromkeys(["".join(words), "".join(core), "-".join(core)]))
    out = []
    for s in slugs:
        out += [f"{s}.{t}" for t in ("com", "ai", "io", "co", "tech", "bio", "systems", "dev")]
        out += [f"{p}{s}.com" for p in ("get", "try", "use")] + [f"{s}hq.com", f"{s}labs.com", f"{s}inc.com"]
    return list(dict.fromkeys(out))


def page_text(client: httpx.Client, url: str) -> tuple[str, str]:
    """(title, visible text) of a page; empty on any failure."""
    try:
        r = client.get(url, timeout=8, follow_redirects=True)
    except httpx.HTTPError:
        return "", ""
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return "", ""
    raw = r.text
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    mailtos = " ".join(re.findall(r'mailto:([^"\'?>\s]+)', raw))   # addresses often live only in mailto links
    return (m.group(1).strip() if m else ""), re.sub(r"\s+", " ", text) + " " + mailtos


def company_domain(client: httpx.Client, company: str, keywords: list[str] | None = None) -> str | None:
    """The candidate domain whose homepage names the company and shares the most of `keywords`
    (distinctive words from the article the person came from). Needs at least 2 shared words when
    keywords are known; a single headline word let unrelated companies with the same name through."""
    core = " ".join(w for w in _words(company) if w not in STOP) or company.lower()
    name_words = set(_words(company))
    keywords = [k for k in (keywords or []) if k.lower() not in name_words]
    best, best_hits = None, 0
    for dom in candidate_domains(company):
        title, text = page_text(client, f"https://{dom}")
        if not text:
            continue
        low = text.lower()
        if core not in title.lower() and core not in low:
            continue
        if not keywords:
            if core in title.lower():
                return dom
            continue
        hits = sum(1 for k in keywords if k.lower() in low)
        if hits > best_hits:
            best, best_hits = dom, hits
    return best if best_hits >= 2 else None


def pattern_candidates(name: str, domain: str) -> list[str]:
    name = re.sub(r"[’'`]", "", name or "")            # D’Souza -> DSouza, not D + Souza
    parts = [p.lower() for p in re.findall(r"[A-Za-z]+", name)]
    if len(parts) < 2:
        return []
    f, l = parts[0], parts[-1]
    return [p.format(f=f, l=l, fi=f[0], li=l[0]) + "@" + domain for p in PATTERNS]


STOPWORDS = set("""about above after again against among because before being below between could doing during
every further having however itself might other ourselves should since their theirs there these those through
under until which while would years today company companies startup startups raised raises raising round funding
million billion seed series reported private investors investor venture ventures capital partners founder founders
cofounder chief executive officer platform customers announced announces according including first build building
built started based market where people across around world help helps working works plans percent""".split())


def keywords_from(texts: list[str], limit: int = 25) -> list[str]:
    """Most frequent distinctive words in the texts the person came from (article, evidence)."""
    from collections import Counter

    words = re.findall(r"[a-z][a-z\-]{4,}", " ".join(texts).lower())
    counts = Counter(w for w in words if w not in STOPWORDS)
    return [w for w, _ in counts.most_common(limit)]
