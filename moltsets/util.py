"""Domain, email and text helpers."""

import re
from urllib.parse import urlparse

from moltsets.config import (
    ACADEMIC_DOMAIN_RE,
    COMPANY_NOISE,
    COUNTRY_ALIASES,
    DOMAIN_RE,
    EMAIL_RE,
    FREEMAIL_DOMAINS,
    LINKEDIN_RE,
    LINKEDIN_SLUG_RE,
    PERSONAL_EMAIL_PROVIDERS,
    RISK_ACTIONS,
)


def domain_from_url(url):
    url = url.strip() if isinstance(url, str) else ""
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = f"https://{url}"
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:  # junk such as "https://[bad" in a CSV cell or an API reply
        return ""


def valid_domain(value):
    domain = domain_from_url(value)
    return domain if len(domain) <= 253 and DOMAIN_RE.match(domain) else ""


def normalize_linkedin(value):
    """Return a canonical profile URL for a LinkedIn URL or bare slug, or None if it isn't one."""
    value = (value or "").strip()
    if m := LINKEDIN_RE.search(value):
        return f"https://www.linkedin.com/in/{m.group(1).rstrip('/')}"
    if LINKEDIN_SLUG_RE.match(value) and "." not in value:
        return f"https://www.linkedin.com/in/{value}"
    return None


def split_terms(value, sep=","):
    return [t.strip() for t in (value or "").split(sep) if t.strip()]


def split_emails(value):
    return [e for e in re.split(r"[;,\s]+", value or "") if EMAIL_RE.match(e)]


def email_domain(email):
    m = EMAIL_RE.match(email or "")
    return m.group(1).lower() if m else ""


def is_personal_domain(domain):
    return bool(domain) and (domain in FREEMAIL_DOMAINS or domain_stem(domain) in PERSONAL_EMAIL_PROVIDERS)


def is_academic_domain(domain):
    return bool(ACADEMIC_DOMAIN_RE.search(domain or ""))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def plural(n, word):
    if n == 1:
        return f"1 {word}"
    if word == "person":
        return f"{n} people"
    if word.endswith("y") and word[-2] not in "aeiou":
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}s"


def normalize_country(value):
    value = value.strip()
    return COUNTRY_ALIASES.get(value.lower(), value if value != value.lower() else value.title())


def match_vocabulary(terms, vocabulary):
    """Map user terms onto a fixed MoltSets vocabulary, case-insensitively. Returns (matched, unmatched)."""
    lookup = {v.lower().replace(" ", ""): v for v in vocabulary}
    matched, unmatched = [], []
    for term in terms:
        value = lookup.get(term.lower().replace(" ", ""))
        if value is None:
            unmatched.append(term)
        elif value not in matched:
            matched.append(value)
    return matched, unmatched


TITLE_ABBREVIATIONS = {"sr": "senior", "jr": "junior", "mgr": "manager", "asst": "assistant", "dy": "deputy",
                       "gm": "general manager", "vp": "vice president", "avp": "assistant vice president",
                       "scm": "supply chain management", "&": "and", "of": ""}


def _title_words(title):
    words = re.sub(r"[^a-z0-9&]+", " ", (title or "").lower()).split()
    return {part for w in words for part in TITLE_ABBREVIATIONS.get(w, w).split()}


def titles_differ(old, new):
    """True when two job titles share under half their words, so rewordings like
    "Head of Logistics and Wholesale Business" vs "Head - Logistics OE/Wholesale Business" don't count."""
    a, b = _title_words(old), _title_words(new)
    if not (a and b):
        return False
    return len(a & b) / len(a | b) < 0.5


def _norm_company(name):
    name = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return " ".join(COMPANY_NOISE.sub(" ", name).split())


def domain_stem(domain):
    """The organisation part of a domain: careers.examplefoods.example -> examplefoods, acme.co.example -> acme."""
    parts = (domain or "").lower().split(".")[:-1]
    if len(parts) > 1 and parts[-1] in ("co", "com", "net", "org", "gov", "ac", "edu"):
        parts = parts[:-1]
    return parts[-1] if parts else ""


def same_company(expected_name, expected_domain, current_name, current_domain):
    """True / False if we can tell whether two employers are the same, None if we can't."""
    checks = []
    if expected_domain and current_domain:
        checks.append(domain_stem(expected_domain) == domain_stem(current_domain))
    a, b = _norm_company(expected_name), _norm_company(current_name)
    if a and b:
        checks.append(a in b or b in a)
    if not checks:
        return None
    return any(checks)


def email_record(email, risk, validated_at, email_type, source):
    risk = risk or "F"  # MoltSets: treat a missing grade as F
    return {
        "email": email,
        "email_type": email_type,
        "email_risk_score": risk,
        "email_action": RISK_ACTIONS.get(risk, "your call"),
        "email_validated_at": validated_at,
        "email_source": source,
    }


EMPTY_EMAIL = dict.fromkeys(email_record("", None, None, None, None))


def assess_email(record, company_domain, company_email_domains=()):
    """Check an email against the company it's meant to reach. Returns (record, note or None).

    MoltSets' grade measures deliverability only, so a graded-A address can still be a personal
    inbox, a university alumni address, or a previous employer's. Personal inboxes are labelled
    `personal` whatever MoltSets says; academic and other-company addresses get a "verify first" action.
    """
    if not record:
        return record, None
    domain = email_domain(record["email"])
    if is_personal_domain(domain):
        return {**record, "email_type": "personal"}, None
    if record["email_risk_score"] == "D" or not company_domain:
        return record, None
    if domain in company_email_domains:
        return record, None  # an email domain several of the company's people use (e.g. acme-aero.example for acme.example)
    if is_academic_domain(domain):
        return {**record, "email_type": "academic", "email_action": "verify first (university address)"}, \
            f"{record['email']} is a university address, not {company_domain}"
    if domain_stem(domain) != domain_stem(company_domain):
        return {**record, "email_action": f"verify first (not a {company_domain} address)"}, \
            f"{record['email']} isn't a {company_domain} address — likely an old employer's"
    return record, None
