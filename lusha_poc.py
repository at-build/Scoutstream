"""
POC: given one or more companies (name + website), use the Lusha API to find
leads in one or more departments (e.g. purchasing / supply chain), optionally
filtered by seniority.

Workflow (Lusha v3 REST API, https://docs.lusha.com/apis/openapi):
  1. GET  /v3/contacts/prospecting/filters/departments   -> resolve department filter values
     GET  /v3/contacts/prospecting/filters/seniority     -> resolve seniority filter values
     GET  /v3/contacts/prospecting/filters/locations     -> resolve location text to a location object
  2. POST /v3/contacts/prospecting                        -> discover contacts at the company,
                                                              filtered by department/seniority
  3. POST /v3/contacts/enrich                              -> reveal emails (and phones with
                                                              --phones) for the top prospects

Note: Lusha's "Search & Enrich" endpoint (/v3/contacts/search-and-enrich) only enriches
contacts you can already identify by name+company or LinkedIn URL — it can't discover
*unknown* people at a company by department. Prospecting + Enrich is the equivalent
discovery workflow: find candidates, then reveal their contact info.

Auth: API key is read from LUSHA_API_KEY, loaded from a local .env file (or the
environment) and sent as the `api_key` header.

Usage:
  # one-time setup: put your key in .env
  echo 'LUSHA_API_KEY=your_key_here' >> .env

  python3 lusha_poc.py --company "Example Wind Group" --website "https://www.examplewind.example/" \\
      --departments purchasing,"supply chain"     # seniority defaults to "director+"; "all" = every level

  # multiple companies: repeat --company/--website (paired in order) ...
  python3 lusha_poc.py --company "Example Wind Group" --website examplewind.example \\
      --company "Example Electric" --website exampleelectric.example

  # ... or pass a CSV with `company` and `website` header columns
  python3 lusha_poc.py --companies-file companies.csv

  # geography: only people based in a given place (country, state, city, continent)
  python3 lusha_poc.py --company "Example Home" --website examplehome.example --departments purchasing --location USA
  # (in --companies-file, an optional `location` column sets it per company)

Pre-search check: before any (billed) search, the script checks that the CSV has
its required columns, every row has a company and a valid website, and that the
department / seniority / location terms (including per-row locations) all match
Lusha filter values (free lookups). Any problem is listed with what continuing
would do, and you're asked whether to continue. If you don't, the script waits
(no searches run) for you to fix or replace the CSV, then re-checks it.

With multiple companies, filters are resolved once, each company is searched
separately (--max-results applies per company), and all leads are written to a
single CSV with a `target_company` column identifying which input they came from.

Credit cap: only MAX_REVEALS_PER_COMPANY (5) prospects per company are revealed
via /contacts/enrich, however many the search returns. Prospects are ranked by
how closely their department/job title and seniority match the requested filters
(see relevance_key) and the top 5 are revealed; the rest are still written to the
CSV, ranked, without contact info (contact_revealed = False). Search results say
what Lusha has on file (`has`) and what a reveal would cost (`canReveal`), so:
  - prospects with no email on file (no email/phone with --phones) are never
    revealed — the next-best match gets the slot instead;
  - contacts your account already revealed cost 0 credits, so they're re-revealed
    on top of the cap.
--require-email goes further and only searches for people with a known work
email (Lusha existingDataPoints filter), at the cost of dropping the rest from the CSV.

Lusha pricing (GET /v3/account/usage): search = 1 credit per 25 results, email
reveal = 1 credit, phone reveal = 5 credits. Only emails are revealed unless
--phones is passed, so a company costs ~6 credits by default (~31 with --phones).
"""

import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from moltsets.safety import clean, clean_text, write_table

load_dotenv()

BASE_URL = "https://api.lusha.com/v3"
API_KEY = os.environ.get("LUSHA_API_KEY")

MAX_PAGE_SIZE = 100  # /v3/contacts/prospecting pagination.size max
# Search is billed per result returned, so default well below the page-size max.
DEFAULT_MAX_RESULTS = 25
MAX_ENRICH_BATCH = 100  # /v3/contacts/enrich ids max per request
# Revealing via /contacts/enrich is billed per field (email 1 credit, phone 5),
# so only the N closest-matching prospects per company are revealed.
MAX_REVEALS_PER_COMPANY = 5
MAX_RATE_LIMIT_WAIT = 120  # seconds; a longer wait asked for by the server is cut to this
# --require-email: Lusha existingDataPoints value (live values: phone, direct_phone,
# mobile_phone, unknown_phone, no_dnc_phone, email, work_email, private_email)
REQUIRE_EMAIL_DATA_POINT = "work_email"

# Lusha's department taxonomy has no literal "procurement"/"purchasing"/
# "supply chain" value (confirmed live: GET /contacts/prospecting/filters/departments
# returns a fixed list like "Operations", "Sales", "Engineering & Technical" ...).
# Procurement/supply-chain roles are classified under "Operations", but that
# bucket is broad (hundreds of people per company), so these terms are NOT
# matched against the department taxonomy — they drive the job-title filter
# below instead, which is what actually narrows results precisely.
DEPARTMENT_KEYWORD_HINTS = {
    "operations": ["operations"],
}

# Job-title keywords used to narrow results to procurement/supply-chain roles
# specifically (departments alone are too coarse for this). Matched via
# Lusha's `jobTitles` filter, which does a substring-style match server-side.
JOB_TITLE_HINTS = {
    "purchasing": ["Procurement", "Purchasing", "Sourcing", "Buyer"],
    "procurement": ["Procurement", "Purchasing", "Sourcing", "Buyer"],
    "supply chain": ["Supply Chain", "Logistics", "Materials"],
    "supply-chain": ["Supply Chain", "Logistics", "Materials"],
    "logistics": ["Logistics", "Supply Chain"],
}

# Lusha's seniority levels (GET /contacts/prospecting/filters/seniority, confirmed
# live): founder 10, c-suite 9, vice president 8, partner 7, director 6, manager 5,
# senior 4, entry 3, intern 2, other 1. Note Lusha's own "senior" (4) is a senior
# individual contributor, *below* manager — so "senior" is not mapped to leadership
# here; it falls through and matches Lusha's "senior" level literally.
DIRECTOR_AND_ABOVE = ["director", "partner", "vice president", "c-suite", "founder"]
SENIORITY_KEYWORD_HINTS = {
    "director+": DIRECTOR_AND_ABOVE,
    "leadership": DIRECTOR_AND_ABOVE,
    # exact Lusha labels: a broader hint like "president" would also fuzzy-match "vice president"
    "executive": ["c-suite", "founder"],
    "manager": ["manager"],
    "junior": ["junior", "entry"],
}
DEFAULT_SENIORITY = "director+"

# Seniority ladder used to rank prospects by how close they are to the requested
# seniority (see relevance_key); levels follow Lusha's order. Checked top to bottom
# so "Vice President" isn't read as "President", "Senior Manager" counts as a
# manager, and "Channel Partner Manager" as a manager rather than a partner.
SENIORITY_LEVELS = [
    (6, ["vice president", "vp"]),
    (7, ["c-suite", "chief", "president", "owner", "founder"]),
    (4, ["director", "head"]),
    (3, ["manager"]),
    (5, ["partner"]),
    (2, ["senior"]),
    (1, ["entry", "junior"]),
    (0, ["intern", "trainee"]),
]
ALL_SENIORITY_TERMS = {"all", "any"}  # --seniority all -> no seniority filter

DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def domain_from_url(url):
    if not re.match(r"^https?://", url):
        url = f"https://{url}"
    try:
        domain = urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:  # junk such as "https://[bad" in a CSV cell
        return ""
    return domain if len(domain) <= 253 else ""


def _headers():
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "api_key": API_KEY,
    }


def _request(method, path, **kwargs):
    """Send a request, waiting out Lusha's per-minute rate limit (the daily limit is not retried)."""
    for _ in range(3):
        # No redirects: requests would resend the api_key header to whatever host a redirect names.
        resp = requests.request(method, f"{BASE_URL}{path}", headers=_headers(), timeout=30,
                                allow_redirects=False, **kwargs)
        if resp.status_code != 429 or "Minute" not in resp.text:
            return resp
        wait = int(m.group(1)) + 1 if (m := re.search(r"Reset in (\d{1,6}) seconds", resp.text)) else 60
        wait = min(wait, MAX_RATE_LIMIT_WAIT)
        print(f"   Per-minute rate limit hit; waiting {wait}s ...")
        time.sleep(wait)
    return resp


def _json(resp, method, path):
    """The reply as JSON, or an HTTPError. Lusha's text is third-party data (names and job titles are written
    by the people themselves), so it's cleaned of control and invisible characters before anything prints it."""
    if not 200 <= resp.status_code < 300:
        print(f"\nERROR calling {method} {path}: HTTP {resp.status_code}")
        print(clean_text(resp.text, 500))
        resp.raise_for_status()
        raise requests.exceptions.HTTPError(f"unexpected HTTP {resp.status_code} (redirects aren't followed)",
                                            response=resp)
    data = clean(resp.json())
    if not isinstance(data, dict):
        raise requests.exceptions.HTTPError(f"unexpected reply from {method} {path}", response=resp)
    return data


def _post(path, payload):
    return _json(_request("POST", path, json=payload), "POST", path)


def _get(path, params=None):
    return _json(_request("GET", path, params=params), "GET", path)


def _filter_option_label(option):
    """Filter option values may come back as plain strings or {id, name}-style dicts."""
    if isinstance(option, str):
        return option, option
    if isinstance(option, dict):
        label = option.get("name") or option.get("label") or option.get("value")
        key = option.get("id", label)
        return key, label
    return option, str(option)


def fetch_filter_options(filter_type):
    data = _get(f"/contacts/prospecting/filters/{filter_type}")
    values = data.get("values") or []
    return [_filter_option_label(v) for v in values]


def resolve_filter_terms(raw_terms, keyword_hints, options):
    """Fuzzy-match user-supplied terms against live filter option labels.

    Only returns terms that actually matched a real option — Lusha's API
    treats an unrecognized enum value in a filter array as invalidating the
    *entire* query (zero results), so unmatched terms are dropped rather than
    passed through raw.
    """
    resolved = []  # list of (key_to_send, label) tuples, deduped by key
    seen_keys = set()

    def add(key, label):
        if key not in seen_keys:
            seen_keys.add(key)
            resolved.append((key, label))

    for term in raw_terms:
        term = term.strip().lower()
        if not term:
            continue
        hints = keyword_hints.get(term, [term])
        for hint in hints:
            for key, label in options:
                label_lower = label.lower()
                if hint in label_lower or label_lower in hint:
                    add(key, label)

    return resolved


def resolve_job_titles(raw_terms, keyword_hints):
    titles = []
    for term in raw_terms:
        term = term.strip().lower()
        for hint in keyword_hints.get(term, []):
            if hint not in titles:
                titles.append(hint)
    return titles


def seniority_level(text):
    """Map a seniority label or job title onto SENIORITY_LEVELS, or None if unrecognized."""
    text = (text or "").lower()
    for level, keywords in SENIORITY_LEVELS:
        if any(re.search(rf"\b{re.escape(k)}\b", text) for k in keywords):
            return level
    return None


def requested_seniority_levels(raw_terms):
    levels = set()
    for term in raw_terms:
        term = term.strip().lower()
        for hint in SENIORITY_KEYWORD_HINTS.get(term, [term]):
            level = seniority_level(hint)
            if level is not None:
                levels.add(level)
    return levels


def relevance_key(prospect, department_labels, job_titles, seniority_levels):
    """Sort key ranking which prospects to reveal first (lower sorts first).

    1. Department fit — 0 if the job title contains a requested job-title keyword,
       1 if only Lusha's department matches, 2 if neither.
    2. Distance from the requested seniority on SENIORITY_LEVELS (0 = inside the
       requested range, e.g. anything director-and-above for "director+").
    3. Higher seniority first among equally close matches, e.g. C-suite/founder,
       then VP, then partner, then director.
    Seniority comes from the prospect's Lusha label, falling back to its job title;
    unknown seniority ranks last.
    """
    job = prospect.get("jobTitle") or {}
    title = (job.get("title") or "").lower()
    departments = {str(d).lower() for d in job.get("departments") or []}
    if any(k.lower() in title for k in job_titles):
        department_fit = 0
    elif departments & {label.lower() for label in department_labels}:
        department_fit = 1
    else:
        department_fit = 2

    raw = job.get("seniority")
    level = seniority_level(_filter_option_label(raw)[1]) if raw else None
    if level is None:
        level = seniority_level(title)
    seniority_distance = 0
    if seniority_levels:
        seniority_distance = (
            min(abs(level - r) for r in seniority_levels) if level is not None else len(SENIORITY_LEVELS)
        )
    seniority_rank = -level if level is not None else 1  # unknown sorts after every known level
    return department_fit, seniority_distance, seniority_rank


def revealable_fields(prospect, reveal_fields):
    """Requested fields Lusha has on file for this prospect, per the search result's `has` list.

    Returns reveal_fields unchanged if the result has no `has` list, so missing
    metadata never blocks a reveal.
    """
    has = prospect.get("has")
    if has is None:
        return list(reveal_fields)
    return [f for f in reveal_fields if f in has]


def reveal_is_free(prospect, reveal_fields):
    """True if the search result's `canReveal` prices every requested field at 0 credits,
    i.e. your account has already revealed this contact and re-revealing is free."""
    items = [c for c in prospect.get("canReveal") or [] if c.get("field") in reveal_fields]
    return bool(items) and all(c.get("credits") == 0 for c in items)


def resolve_location(text, cache):
    """Resolve free text ("USA", "California", "Bengaluru") to a Lusha location object.

    Uses Lusha's location lookup, which handles aliases ("US", "UK") and returns
    objects already in the prospecting filter shape, e.g.
    {"continent": "North America", "country": "United States"}. Takes Lusha's top
    match ("California" -> the US state, not the Colombian city). Returns None if
    nothing matched.
    """
    key = text.strip().lower()
    if key not in cache:
        values = _get("/contacts/prospecting/filters/locations", {"query": text.strip()}).get("values") or []
        cache[key] = values[0] if values else None
    return cache[key]


def resolve_locations(terms, cache):
    """Resolve each term, returning (locations, unmatched_terms)."""
    locations, unmatched = [], []
    for term in terms:
        location = resolve_location(term, cache)
        if location is None:
            unmatched.append(term)
        elif location not in locations:
            locations.append(location)
    return locations, unmatched


def format_location(location):
    return ", ".join(location[k] for k in ("city", "state", "country", "continent") if location.get(k))


def prospecting_search(company_name, domain, search_filters, locations, page_size):
    """search_filters is the dict from resolve_search_filters."""
    department_keys = search_filters["department_keys"]
    job_titles = search_filters["job_titles"]
    seniority_ids = search_filters["seniority_ids"]
    filters = {
        "contacts": {"include": {}},
        "companies": {"include": {"domains": [domain], "names": [company_name]}},
    }
    if department_keys:
        filters["contacts"]["include"]["departments"] = department_keys
    if job_titles:
        filters["contacts"]["include"]["jobTitles"] = job_titles
    if seniority_ids:
        filters["contacts"]["include"]["seniorityIds"] = seniority_ids
    if locations:
        # contact location = where the person is based (not the company HQ)
        filters["contacts"]["include"]["locations"] = locations
    if search_filters["existing_data_points"]:
        filters["contacts"]["include"]["existingDataPoints"] = search_filters["existing_data_points"]

    payload = {
        "pagination": {"page": 0, "size": page_size},
        "filters": filters,
        "options": {"includePartialProfiles": True},
    }
    print(f"2. Searching prospects at {company_name} ({domain}) ...")
    print(
        f"   Departments: {department_keys or '(none)'}  "
        f"Job titles: {job_titles or '(none)'}  Seniority ids: {seniority_ids or '(all levels)'}  "
        f"Location: {[format_location(l) for l in locations] or '(anywhere)'}"
        + ("  Requires: work email" if search_filters["existing_data_points"] else "")
    )
    data = _post("/contacts/prospecting", payload)
    results = [r for r in data.get("results", []) if not r.get("error")]
    pagination = data.get("pagination", {})
    print(f"   Found {len(results)} prospect(s) (total available: {pagination.get('total')}).")
    return results


def enrich_contacts(contact_ids, reveal_fields):
    if not contact_ids:
        return {}
    print(f"3. Revealing {' + '.join(reveal_fields)} for {len(contact_ids)} prospect(s) ...")
    contacts_by_id = {}
    for i in range(0, len(contact_ids), MAX_ENRICH_BATCH):
        batch = contact_ids[i : i + MAX_ENRICH_BATCH]
        # Always send `reveal` explicitly: omitting it makes Lusha reveal (and bill) both fields.
        payload = {"ids": batch, "reveal": reveal_fields}
        try:
            data = _post("/contacts/enrich", payload)
        except requests.exceptions.HTTPError:
            print("   Skipping contact enrichment for this batch (see error above).\n")
            continue
        for row in data.get("results", []):
            if row.get("id"):
                contacts_by_id[row["id"]] = row
    return contacts_by_id


def build_rows(target_company, prospects, contacts_by_id):
    rows = []
    for p in prospects:
        pid = p.get("id")
        enriched = contacts_by_id.get(pid, {})
        job = enriched.get("jobTitle") or p.get("jobTitle") or {}
        location = enriched.get("location") or p.get("location") or {}
        company = enriched.get("company") or p.get("company") or {}
        social = p.get("socialLinks") or {}
        emails = [e.get("email") for e in enriched.get("emails", []) if e.get("email")]
        phones = [ph.get("number") or ph.get("phone") for ph in enriched.get("phones", [])]
        has = p.get("has")
        on_file = "unknown" if has is None else ", ".join(f for f in ("emails", "phones") if f in has)
        rows.append(
            {
                "target_company": target_company,
                "full_name": f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
                "job_title": job.get("title"),
                "job_department": ", ".join(job.get("departments", []) or []),
                "seniority": job.get("seniority"),
                "company": company.get("name"),
                "city": location.get("city"),
                "country": location.get("country"),
                "linkedin": social.get("linkedin"),
                "email": "; ".join(filter(None, emails)),
                "phone": "; ".join(filter(None, phones)),
                "data_on_file": on_file,  # what Lusha can reveal (from search); blank = nothing
                "contact_revealed": pid in contacts_by_id,
            }
        )
    return rows


def split_terms(value):
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def resolve_search_filters(args, location_cache):
    """Check --departments / --seniority / --location against Lusha before any search runs.

    Returns (filters, issues, blocking). Unmatched terms are dropped (Lusha zeroes
    the whole query on an unknown filter value); each issue says what continuing
    without them will do. blocking is True when no department/job-title filter is
    left, since the search would then return everyone at the company.
    """
    issues = []
    print("1. Checking department / seniority / location filters against your Lusha account ...")

    department_options = fetch_filter_options("departments")
    department_matches, job_titles, unmatched = [], [], []
    for term in split_terms(args.departments):
        matches = resolve_filter_terms([term], DEPARTMENT_KEYWORD_HINTS, department_options)
        titles = resolve_job_titles([term], JOB_TITLE_HINTS)
        if not matches and not titles:
            unmatched.append(term)
        department_matches += [m for m in matches if m not in department_matches]
        job_titles += [t for t in titles if t not in job_titles]
    department_labels = [label for _, label in department_matches]
    print(f"   Departments: {department_labels or '(none — using job titles instead)'}")
    print(f"   Job title keywords: {job_titles or '(none)'}")
    if not department_matches and not job_titles:
        known = sorted(JOB_TITLE_HINTS) + [label for _, label in department_options]
        issues.append(
            f"--departments: none of {unmatched} matched a Lusha department or job-title keyword, so there is "
            f"no role filter to search with. Re-run with one of: {known}."
        )
        return None, issues, True
    if unmatched:
        issues.append(
            f"--departments: {unmatched} matched nothing in Lusha — continuing searches only for "
            f"{department_labels + job_titles}."
        )

    seniority_terms = split_terms(args.seniority)
    seniority_ids, seniority_levels = [], set()
    if {t.lower() for t in seniority_terms} & ALL_SENIORITY_TERMS:
        print("   Seniority: all levels")
    else:
        seniority_options = fetch_filter_options("seniority")
        seniority_matches, unmatched = [], []
        for term in seniority_terms:
            matches = resolve_filter_terms([term], SENIORITY_KEYWORD_HINTS, seniority_options)
            if not matches:
                unmatched.append(term)
            seniority_matches += [m for m in matches if m not in seniority_matches]
        seniority_labels = [label for _, label in seniority_matches]
        # seniority is filtered by seniorityIds (integers) per the API schema
        seniority_ids = [int(k) for k, _ in seniority_matches if str(k).lstrip("-").isdigit()]
        seniority_levels = requested_seniority_levels(seniority_terms)
        print(f"   Seniority: {seniority_labels or '(none)'}")
        if seniority_matches and not seniority_ids:
            issues.append(
                "--seniority: Lusha's seniority values came back without numeric ids, so the seniority filter "
                "can't be applied — continuing searches all seniority levels (results are still ranked by seniority)."
            )
        if unmatched:
            continuing = f"only {seniority_labels}" if seniority_ids else "all seniority levels"
            issues.append(
                f"--seniority: {unmatched} matched no Lusha seniority level — continuing searches {continuing}. "
                f"Known terms: {sorted(SENIORITY_KEYWORD_HINTS) + sorted(ALL_SENIORITY_TERMS)} or a Lusha level: "
                f"{[label for _, label in seniority_options]}."
            )

    default_locations = []
    if args.location:
        default_locations, unmatched = resolve_locations(args.location, location_cache)
        print(f"   Location: {[format_location(l) for l in default_locations] or '(no match)'}")
        if unmatched and default_locations:
            issues.append(
                f"--location: no Lusha location matched {unmatched} — continuing searches only "
                f"{[format_location(l) for l in default_locations]}."
            )
        elif unmatched:
            issues.append(
                f"--location: no Lusha location matched {unmatched} — companies without their own `location` "
                "value will be skipped (they are not searched worldwide)."
            )
    else:
        print("   Location: anywhere (unless set per company)")

    existing_data_points = []
    if args.require_email:
        data_point_options = [label for _, label in fetch_filter_options("existingDataPoints")]
        if REQUIRE_EMAIL_DATA_POINT in data_point_options:
            existing_data_points = [REQUIRE_EMAIL_DATA_POINT]
            print("   Require email: only prospects with a known work email")
        else:
            issues.append(
                f"--require-email: Lusha doesn't offer a {REQUIRE_EMAIL_DATA_POINT!r} filter on your account "
                f"(available: {data_point_options}) — continuing without it; prospects with no email on file "
                "are still listed but not revealed."
            )

    filters = {
        "department_keys": [k for k, _ in department_matches],
        "department_labels": department_labels,
        "job_titles": job_titles,
        "seniority_ids": seniority_ids,
        "seniority_levels": seniority_levels,
        "default_locations": default_locations,
        "existing_data_points": existing_data_points,
    }
    return filters, issues, False


def load_companies(args, companies_file, filters, location_cache):
    """Read and validate the companies to search, before any (billed) Lusha search runs.

    Companies come from companies_file (required `company` and `website` columns,
    optional `location` column with ";"-separated places that overrides --location
    for that row) plus any --company/--website pairs.

    Returns (companies, issues, blocking): companies is a list of
    (company_name, domain, locations) ready to search; issues lists every problem
    found, each saying what continuing will do; blocking is True when nothing can
    be searched (unreadable file, missing columns, no valid companies left).
    """
    entries = []  # (where, company_name, website, location_terms)
    if companies_file:
        try:
            with open(companies_file, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                columns = {(h or "").strip().lower(): h for h in reader.fieldnames or []}
                missing = [c for c in ("company", "website") if c not in columns]
                if missing:
                    found = [h for h in reader.fieldnames or [] if h]
                    return [], [f"{companies_file} is missing required column(s) {missing} (found: {found})."], True
                for line_no, row in enumerate(reader, start=2):
                    location = (row.get(columns["location"]) or "") if "location" in columns else ""
                    entries.append(
                        (
                            f"line {line_no}",
                            clean_text(row.get(columns["company"]) or "").strip(),
                            clean_text(row.get(columns["website"]) or "").strip(),
                            [t.strip() for t in clean_text(location).split(";") if t.strip()],
                        )
                    )
        except OSError as e:
            return [], [f"Can't read {companies_file}: {e.strerror}."], True
        except (UnicodeDecodeError, csv.Error) as e:
            return [], [f"Can't parse {companies_file} ({e}). In Excel, save it as 'CSV UTF-8'."], True
    entries += [("--company", name, website, []) for name, website in zip(args.company, args.website)]

    companies, issues, relies_on_bad_location = [], [], []
    for where, name, website, location_terms in entries:
        if not (name or website or location_terms):
            continue  # blank row
        label = f"{where} ({name})" if name else where
        if not name:
            issues.append(f"{label}: missing company name — row will be skipped.")
            continue
        if not website:
            issues.append(f"{label}: missing website — company will be skipped.")
            continue
        domain = domain_from_url(website)
        if not DOMAIN_RE.match(domain):
            issues.append(f"{label}: website {website!r} isn't a valid domain — company will be skipped.")
            continue
        locations = filters["default_locations"]
        if location_terms:
            try:
                locations, unmatched = resolve_locations(location_terms, location_cache)
            except requests.exceptions.RequestException as e:
                issues.append(f"{label}: couldn't check location {location_terms} ({e}) — company will be skipped.")
                continue
            if unmatched and not locations:
                issues.append(f"{label}: no Lusha location matched {unmatched} — company will be skipped.")
                continue
            if unmatched:
                issues.append(
                    f"{label}: no Lusha location matched {unmatched} — searching only "
                    f"{[format_location(l) for l in locations]}."
                )
        elif args.location and not locations:
            relies_on_bad_location.append(name)
            continue
        companies.append((name, domain, locations))

    if relies_on_bad_location:
        issues.append(f"Skipped because --location matched nothing: {relies_on_bad_location}.")
    if not companies:
        issues.append("No valid companies left to search.")
        return companies, issues, True
    return companies, issues, False


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:  # not running interactively (piped / scheduled)
        print()
        sys.exit("No answer available (non-interactive run); stopped before any Lusha search.")


def report_issues(issues):
    print(f"\n!! Pre-search check found {len(issues)} issue(s):")
    for issue in issues:
        print(f"   - {issue}")


def wait_for_reupload(path, has_filter_issues):
    """Pause (no searches run) until the user has fixed or replaced the companies CSV.

    Returns the path to re-check: the same file, or a new one the user typed or dragged in.
    """
    print(f"\nSearch not started. Fix and save {path}, or prepare a corrected CSV.")
    if has_filter_issues:
        print("(Problems with --departments / --seniority / --location need a re-run with different flags: quit and re-run.)")
    answer = ask("Press Enter to re-check it, type or drag in the path of a new CSV, or 'q' to quit: ")
    if answer.lower() in ("q", "quit", "exit"):
        print("Stopped. No Lusha searches were run.")
        sys.exit(0)
    # terminals quote or backslash-escape dragged-in paths
    return os.path.expanduser(answer.strip("'\"").replace("\\ ", " ")) if answer else path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--company",
        action="append",
        default=[],
        help='Company name, e.g. "Example Wind Group". Repeat for multiple companies.',
    )
    parser.add_argument(
        "--website",
        action="append",
        default=[],
        help='Company website, e.g. "https://www.examplewind.example/". Repeat once per --company, in the same order.',
    )
    parser.add_argument(
        "--companies-file",
        default=None,
        help="CSV file of companies to search, with `company` and `website` header columns and an optional "
        "`location` column (overrides --location for that row; separate several places with ';'). "
        "Checked before any search; problems are flagged and you're asked whether to continue.",
    )
    parser.add_argument(
        "--location",
        action="append",
        default=[],
        help='Only find people based in this place, e.g. "USA", "California", "Bengaluru", "Europe". '
        "Repeat for several places. Resolved against Lusha's location lookup.",
    )
    parser.add_argument(
        "--departments",
        default="purchasing,supply chain",
        help='Comma-separated departments to target (default: "purchasing,supply chain"). '
        "Everyday terms are fuzzy-matched against Lusha's live department taxonomy.",
    )
    parser.add_argument(
        "--seniority",
        default=DEFAULT_SENIORITY,
        help=f'Comma-separated seniority level(s). Default: "{DEFAULT_SENIORITY}" (= "leadership": director, '
        'partner, VP, C-suite, founder). Other terms: "executive" (C-suite, founder), "manager", "junior", '
        '"all" (every level), '
        'or a Lusha level name, e.g. "senior" (Lusha\'s senior individual contributor, below manager).',
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Max prospects to search per company (10-100); only the {MAX_REVEALS_PER_COMPANY} "
        "closest matches are revealed, the rest are saved without contact info",
    )
    parser.add_argument(
        "--require-email",
        action="store_true",
        help="Only search for prospects Lusha has a work email for. Every lead can then be revealed, but "
        "people without an email are left out of the CSV entirely (by default they're listed, just not revealed).",
    )
    parser.add_argument(
        "--phones",
        action="store_true",
        help="Also reveal phone numbers. Off by default: Lusha charges 5 credits per phone vs 1 per email.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path (default: <company>_lusha_leads.csv for one company, lusha_leads.csv for several)",
    )
    args = parser.parse_args()
    if len(args.company) != len(args.website):
        parser.error("pass exactly one --website for every --company (they are paired in order)")
    if not args.company and not args.companies_file:
        parser.error("provide --company/--website or --companies-file")
    return args


def main():
    if not API_KEY:
        print("ERROR: LUSHA_API_KEY is not set.")
        print("Add it to a .env file in this directory: LUSHA_API_KEY=your_key_here")
        sys.exit(1)

    args = parse_args()
    reveal_fields = ["emails", "phones"] if args.phones else ["emails"]

    # Pre-search check: nothing billed runs until the filters and companies are
    # validated (Lusha filter lookups are free) and any problems are confirmed.
    location_cache = {}
    search_filters, filter_issues, filter_blocking = resolve_search_filters(args, location_cache)
    if filter_blocking:
        report_issues(filter_issues)
        sys.exit("Stopped before any Lusha search. Fix the filter(s) above and re-run.")
    companies_file = args.companies_file
    while True:
        if companies_file:
            print(f"\nChecking {companies_file} ...")
        companies, company_issues, company_blocking = load_companies(
            args, companies_file, search_filters, location_cache
        )
        issues = filter_issues + company_issues
        if not issues:
            print(f"   Pre-search check passed: {len(companies)} compan{'y' if len(companies) == 1 else 'ies'} ready.")
            break
        report_issues(issues)
        if not company_blocking:
            n = f"{len(companies)} compan{'y' if len(companies) == 1 else 'ies'}"
            if ask(f"\nContinue anyway and search {n}? [y/N] ").lower() in ("y", "yes"):
                break
        if not companies_file:
            sys.exit("Stopped before any Lusha search. Fix the issue(s) above and re-run.")
        companies_file = wait_for_reupload(companies_file, bool(filter_issues))

    if args.output:
        output_csv = args.output
    elif len(companies) == 1:
        output_csv = f"{re.sub(r'[^a-z0-9]+', '_', companies[0][0].lower()).strip('_')}_lusha_leads.csv"
    else:
        output_csv = "lusha_leads.csv"
    print(f"\nSearching {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}; revealing {' + '.join(reveal_fields)}.")

    department_labels = search_filters["department_labels"]
    all_rows = []
    summary = []  # (company_name, status line)
    for i, (company_name, domain, locations) in enumerate(companies, start=1):
        print(f"\n##### [{i}/{len(companies)}] {company_name} #####")
        try:
            prospects = prospecting_search(company_name, domain, search_filters, locations, args.max_results)
        except requests.exceptions.RequestException as e:
            status = getattr(e.response, "status_code", None)
            if status in (402, 403, 429):
                # out of credits / rate-limited: later searches would fail too, so stop and save what we have
                print(f"   Stopping at {company_name}: HTTP {status} (out of credits or rate-limited).")
                summary += [(name, "not searched (stopped)") for name, _, _ in companies[i - 1 :]]
                break
            print(f"   Skipping {company_name}: {e}")
            summary.append((company_name, "search failed"))
            continue
        ranked = sorted(
            prospects,
            key=lambda p: relevance_key(
                p, department_labels, search_filters["job_titles"], search_filters["seniority_levels"]
            ),
        )
        # Only reveal prospects Lusha actually has the requested data for; re-revealing
        # contacts this account already paid for is free, so those don't count toward the cap.
        revealable = [p for p in ranked if p.get("id") and revealable_fields(p, reveal_fields)]
        free = [p for p in revealable if reveal_is_free(p, reveal_fields)]
        paid = [p for p in revealable if p not in free][:MAX_REVEALS_PER_COMPANY]
        to_reveal = [p for p in ranked if p in free or p in paid]
        unrevealed = [p for p in ranked if p not in to_reveal]
        nothing_on_file = len(ranked) - len(revealable)
        over_cap = len(revealable) - len(free) - len(paid)
        if nothing_on_file:
            wanted = " or ".join(f.rstrip("s") for f in reveal_fields)
            print(f"   {nothing_on_file} prospect(s) have no {wanted} on file in Lusha — not revealed.")
        if free:
            print(f"   {len(free)} already revealed by your account — re-revealing for free (not counted in the cap).")
        if over_cap:
            print(
                f"   Revealing the {len(paid)} closest match(es) to control credit usage; {over_cap} more with "
                "data on file are saved without contact info."
            )
        try:
            contacts_by_id = enrich_contacts([p["id"] for p in to_reveal], reveal_fields)
        except requests.exceptions.RequestException as e:
            # e.g. out of credits: keep the prospects (unrevealed) rather than losing the whole run
            print(f"   Reveal failed for {company_name}: {e} — saving prospects without contact info.")
            contacts_by_id = {}
        rows = build_rows(company_name, to_reveal + unrevealed, contacts_by_id)
        revealed_rows = [r for r in rows if r["contact_revealed"]]
        summary.append((company_name, f"{len(rows)} prospect(s), {len(revealed_rows)} revealed"))

        if not rows:
            print(f"\nNo leads found for departments {department_labels} at {company_name}.")
            continue

        print(f"\n=== {len(rows)} prospect(s) found at {company_name}, {len(revealed_rows)} revealed ===\n")
        for r in revealed_rows:
            print(f"- {r['full_name']} — {r['job_title']} ({r['job_department']}, {r['seniority']})")
            print(f"  Location: {r['city']}, {r['country']}")
            print(f"  LinkedIn: {r['linkedin']}")
            print(f"  Email:    {r['email'] or '(none returned)'}")
            if args.phones:
                print(f"  Phone:    {r['phone'] or '(none returned)'}")
            print()
        if len(rows) > len(revealed_rows):
            print(f"  + {len(rows) - len(revealed_rows)} more prospect(s) saved to CSV without contact info.\n")
        all_rows.extend(rows)

    if len(companies) > 1:
        print("\n=== Summary ===")
        for company_name, status in summary:
            print(f"  {company_name}: {status}")

    if not all_rows:
        print("\nNo leads found; no CSV written.")
        return

    write_table(output_csv, list(all_rows[0].keys()), all_rows)  # formula-safe cells, for spreadsheets
    print(f"\nSaved {len(all_rows)} lead(s) to {output_csv}")


if __name__ == "__main__":
    main()
