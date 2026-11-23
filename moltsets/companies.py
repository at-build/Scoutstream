"""Company search helpers shared by the company modes."""

from moltsets.api import call
from moltsets.config import EMPLOYEE_RANGES, INDUSTRIES, REVENUE_RANGES
from moltsets.util import match_vocabulary, normalize_country, split_terms, valid_domain


def company_filters(args):
    """Build search_companies filters from the flags. Returns (filters, issues, blocking)."""
    filters, issues = {}, []
    for flag, key, vocabulary in (
        ("industry", "industry", INDUSTRIES),
        ("employee_range", "employee_range", EMPLOYEE_RANGES),
        ("revenue_range", "revenue_range", REVENUE_RANGES),
    ):
        terms = split_terms(getattr(args, flag), ";" if flag == "industry" else ",")
        if flag == "industry" and len(terms) == 1 and "," in terms[0] and not match_vocabulary(terms, vocabulary)[0]:
            terms = split_terms(terms[0])  # comma-separated industries are fine when none contain a comma
        matched, unmatched = match_vocabulary(terms, vocabulary)
        if unmatched:
            issues.append(
                f"--{flag.replace('_', '-')}: {unmatched} aren't MoltSets values and are dropped. Valid: {vocabulary}."
            )
        if matched:
            filters[key] = matched
    if args.linkedin_industry:
        filters["linkedin_industry"] = split_terms(args.linkedin_industry, ";")
    if args.naics:
        filters["naics_code"] = split_terms(args.naics)
    if args.country:
        filters["country"] = [normalize_country(c) for c in split_terms(args.country)]
    if args.state:
        filters["state"] = split_terms(args.state)
    if args.name:
        filters["query"] = args.name
    if args.domain:
        domains = [d for d in (valid_domain(t) for t in split_terms(args.domain)) if d]
        if domains:
            filters["domain"] = domains
    if not filters:
        issues.append(
            "No usable company filter: pass at least one of --industry, --linkedin-industry, --naics, "
            "--employee-range, --revenue-range, --country, --state, --name or --domain."
        )
        return filters, issues, True
    return filters, issues, False


def company_row(c, location):
    """A search_companies result as an accounts-format row: company, website, location, then firmographics."""
    domain = c.get("domain") or ""
    return {
        "company": c.get("name"),
        "website": f"https://{domain}" if domain else "",
        "location": location,
        "domain": domain,
        "industry": c.get("industry"),
        "linkedin_industry": "; ".join(c.get("linkedin_industry") or []),
        "naics": "; ".join(c.get("naics_titles") or []),
        "employee_range": c.get("employee_range"),
        "employee_count": c.get("employee_count"),
        "revenue_range": c.get("revenue_range"),
        "city": c.get("city"),
        "state": c.get("state"),
        "country": c.get("country"),
        "linkedin_url": c.get("linkedin_url"),
        "moltsets_score": c.get("_score"),
    }


def lookup_company(domain):
    """Firmographics for one domain: the search_companies result, or None if MoltSets doesn't have it.

    1 search result when found, free when not. It's the same payload as prospect's empty-result check, so the
    two share the cache.
    """
    status, res = call("search_companies", {"domain": domain, "limit": 1})
    found = (res.get("results") or []) if status == "ok" else []
    return found[0] if found else None
