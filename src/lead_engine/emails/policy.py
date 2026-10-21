"""Per-group email rules (docs/DESIGN.md section 6). Enforced when an email is chosen AND again
at send time, so a bad address can never slip through either path."""
from __future__ import annotations

from .. import suppression

# Corporate domains engineers must never be emailed at, beyond their own employer's domain.
CORPORATE_DOMAINS = {
    "google.com", "alphabet.com", "deepmind.com", "meta.com", "fb.com", "facebook.com", "instagram.com",
    "apple.com", "amazon.com", "aws.com", "amazon.de", "microsoft.com", "linkedin.com", "github.com",
    "netflix.com", "nvidia.com", "openai.com", "anthropic.com", "uber.com", "lyft.com", "airbnb.com",
    "stripe.com", "salesforce.com", "oracle.com", "ibm.com", "intel.com", "amd.com", "qualcomm.com",
    "adobe.com", "tesla.com", "spacex.com", "databricks.com", "snowflake.com", "x.com", "twitter.com",
    "bytedance.com", "tiktok.com", "pinterest.com", "snap.com", "dropbox.com", "doordash.com",
    "coinbase.com", "palantir.com", "cisco.com", "vmware.com", "broadcom.com", "samsung.com",
}
PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
                    "me.com", "mac.com", "yahoo.com", "proton.me", "protonmail.com", "pm.me", "fastmail.com",
                    "hey.com", "aol.com", "zoho.com"}


def _employer_domains(person) -> set[str]:
    keys = person.keys() if hasattr(person, "keys") else person
    out = set()
    if "employer_domain" in keys and person["employer_domain"]:
        out.add(person["employer_domain"].lower().lstrip("@"))
    return out


def violates_engineer_rule(person, email: str) -> bool:
    """True if an engineer would be emailed at their employer's or any known corporate domain."""
    if person["segment"] != "engineer":
        return False
    dom = suppression.domain_of(email)
    blocked = CORPORATE_DOMAINS | _employer_domains(person)
    return any(dom == b or dom.endswith("." + b) for b in blocked)


def acceptable(person, email: str, source: str, verification: str) -> tuple[bool, str]:
    """Whether an address may be used for this person.

    verification: deliverable | catch_all | unknown | undeliverable | on_file
    source: published_page | exa_websets | hunter | prospeo | icypeas | warm
    """
    if suppression.is_role_address(email):
        return False, "role address"
    if violates_engineer_rule(person, email):
        return False, "engineer at a corporate domain"
    if person["segment"] == "engineer" and source not in ("published_page", "exa_websets"):
        return False, "engineers: only addresses they published, or Websets"
    if person["segment"] == "creator" and source != "published_page":
        return False, "creators: only a contact published on their own site"
    if source == "pattern_guess":
        if person["segment"] != "founder":
            return False, "address guessing is for founders only"
        # a guess must be confirmed as a real mailbox; catch-all servers accept anything, so not enough
        return (verification == "deliverable"), f"guess verification: {verification}"
    if verification == "deliverable" or verification == "on_file":
        return True, "ok"
    if verification == "catch_all" and source == "published_page":
        return True, "catch-all accepted because the person published it"
    return False, f"verification: {verification}"
