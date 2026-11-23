"""People search at one company: searches, checks (role fit, match confidence, email) and lead rows."""

import re

from moltsets.config import (
    ALL_TERMS,
    FOUNDER_RE,
    FOUNDER_TITLES,
    JOB_TITLE_HINTS,
    NOISY_TITLE_KEYWORDS,
    SENIORITY_LADDER,
    SENIORITY_TERMS,
    TITLE_LEVELS,
    WEAK_TITLE_RE,
)
from moltsets.util import (
    _norm_company,
    assess_email,
    domain_stem,
    email_domain,
    email_record,
    EMPTY_EMAIL,
    is_academic_domain,
    is_personal_domain,
    match_vocabulary,
    normalize_linkedin,
    plural,
    split_terms,
)
from moltsets.api import call, lookup_mobile, paged_search, SearchLimitReached, STATE, ToolError, try_call


def resolve_titles(departments):
    """--departments terms -> title keywords. Returns (titles, literal_terms)."""
    titles, literal = [], []
    for term in split_terms(departments):
        if term.lower() in ALL_TERMS:
            return [], []
        hints = JOB_TITLE_HINTS.get(term.lower())
        if not hints:
            literal.append(term)
        for t in hints or [term]:
            if t.lower() not in (x.lower() for x in titles):
                titles.append(t)
    return titles, literal


def resolve_seniority(value):
    """--seniority -> (levels kept, exclude_seniority list, unmatched terms).

    The level filter is applied as an *exclusion* of the other levels, so the ~40% of
    profiles with no seniority on record still come through (they are ranked by the
    level their title implies instead).
    """
    terms = split_terms(value)
    if not terms or {t.lower() for t in terms} & ALL_TERMS:
        return [], [], []
    keep, unmatched = [], []
    for term in terms:
        levels = SENIORITY_TERMS.get(term.lower()) or match_vocabulary([term], SENIORITY_LADDER)[0]
        if not levels:
            unmatched.append(term)
        keep += [lvl for lvl in levels if lvl not in keep]
    exclude = [lvl for lvl in SENIORITY_LADDER if lvl not in keep] if keep else []
    return keep, exclude, unmatched


def person_level(person):
    """Ladder position from the job title, else MoltSets' seniority field.

    The title wins because the field is unreliable: live results tag a "Sourcing Coordinator" and a
    "Director, Materials Engineering" as "C Suite". MoltSets also returns an undocumented "Founder" level.
    """
    title = (person.get("title") or "").lower()
    for level, keywords in TITLE_LEVELS:
        if any(re.search(rf"\b{re.escape(k)}\b", title) for k in keywords):
            return level
    seniority = person.get("seniority")
    return SENIORITY_LADDER.get(seniority, 6 if seniority == "Founder" else None)


def role_fit(person, keywords, group):
    """("strong" | "weak", reason) — does the title really match the role searched for?"""
    title = (person.get("title") or "").lower()
    if WEAK_TITLE_RE.search(title):
        return "weak", "assistant / intern title"
    if group == "founder":
        return ("strong", "") if FOUNDER_RE.search(title) else ("weak", "not a founder / CEO title")
    if not keywords:
        return "strong", ""
    matched = [k.lower() for k in keywords if k.lower() in title]
    if not matched:
        return "weak", "title has none of the requested keywords"
    if "engineer" in title and set(matched) <= NOISY_TITLE_KEYWORDS:
        return "weak", f"engineer matched only on {matched}"
    return "strong", ""


def match_confidence(person, company, others):
    """("high" | "medium" | "low", reason) — is this person really at the target company?

    MoltSets sometimes attaches unrelated profiles to a domain (seen in testing: a "Co-Founder" or "CEO" on another
    continent from everyone else at the company). Signals, strongest first:
      - the listed employer has another domain -> low;
      - a work email on the company's domain (or a domain several of its people use) -> high;
      - country (only for people without such an email): with the company's HQ country known (the CSV's country column, under
        --ignore-location-column), someone there is fine, someone in the country where most of the
        company's other people are is medium (an office abroad, or a polluted domain), anyone else low.
        Without an HQ country, someone in a country none of the others share is low;
      - the name contains the company's name (a company page, or a founder who named the company:
        "Jane Doe" at Jane Doe Foods) -> medium;
      - no location and no email to confirm the match -> medium.
    """
    employer_domain = (person.get("company") or {}).get("domain")
    if employer_domain and domain_stem(employer_domain) != domain_stem(company["domain"]):
        return "low", f"listed employer is {employer_domain}"
    # A work email on the company's own domain confirms the match, wherever the person is based
    # (founders and executives are often based outside the HQ country).
    work_domain = email_domain(person.get("business_email"))
    if work_domain and (domain_stem(work_domain) == domain_stem(company["domain"])
                        or work_domain in company.get("email_domains", ())):
        return "high", ""
    country = person.get("country")
    if country and not company["countries"]:
        counts = {}
        for o in others:
            if o is not person and o.get("country"):
                counts[o["country"]] = counts.get(o["country"], 0) + 1
        main_country, n = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)
        hq = company.get("hq_countries") or []
        if hq:
            if country not in hq:
                if n >= 2 and country == main_country:
                    return "medium", f"based in {country}; company HQ is {' / '.join(hq)}"
                return "low", f"based in {country}; company HQ is {' / '.join(hq)}"
        elif n >= 2 and country != main_country and counts.get(country, 0) == 0:
            return "low", f"based in {country}; the company's other people are in {main_country}"
    name = (person.get("full_name") or "").lower()
    company_words = {w for w in _norm_company(company["name"]).split() + [domain_stem(company["domain"])] if len(w) >= 4}
    if any(re.search(rf"\b{re.escape(w)}\b", name) for w in company_words):
        return "medium", "name contains the company name (a company page, or a founder it's named after)"
    if not (country or person.get("city") or person.get("business_email")):
        return "medium", "no location or email to confirm the match"
    return "high", ""


CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def relevance_key(person, titles, wanted_levels):
    """Sort key (lower first): title-keyword fit, distance from the wanted seniority, then most senior."""
    title = (person.get("title") or "").lower()
    title_fit = 0 if not titles or any(t.lower() in title for t in titles) else 1
    level = person_level(person)
    wanted = {SENIORITY_LADDER[w] for w in wanted_levels}
    distance = 0
    if wanted:
        distance = min(abs(level - w) for w in wanted) if level is not None else len(SENIORITY_LADDER)
    return title_fit, distance, -level if level is not None else 1, -(person.get("_score") or 0)


CONTACT_PREFIX = "contact_"


SEARCH_STATUS_COLUMN = "contact_search_status"


def contact_columns(lead):
    """A prospect lead row as contact_* columns for the enriched copy of the input CSV."""
    renamed = {"target_company": "contact_search_company", "target_domain": "contact_search_domain",
               "contact_type": "contact_type"}
    return {renamed.get(k, CONTACT_PREFIX + k): v for k, v in lead.items()}


def search_company_people(company, titles, wanted_levels, args):
    """All searches for one company. Returns (people, totals) where each person carries a "_group".

    Role search, in two steps so the paid slots go to leaders first (MoltSets' own ordering doesn't):
      1. people at the wanted seniority levels (positive filter);
      2. remaining slots: people with no seniority on record (~40% of profiles) — excluding *every*
         level returns only those, so nobody comes back twice.
    With --founders N, a separate search for founder / CEO titles, so founders never compete with
    the role search for slots.
    """
    base = {"company_domain": company["domain"]}
    if company["countries"]:
        base["country"] = company["countries"]
    role = {**base, **({"title": titles} if titles else {})}
    steps = []
    if wanted_levels:
        steps.append(("role", {**role, "seniority": wanted_levels}, args.max_results))
        steps.append(("role", {**role, "exclude_seniority": list(SENIORITY_LADDER)}, None))  # None = what's left
    else:
        steps.append(("role", role, args.max_results))
    if args.founders:
        steps.append(("founder", {**base, "title": FOUNDER_TITLES}, args.founders))

    people, totals, role_found = [], {}, 0
    for group, filters, limit in steps:
        if limit is None:
            limit = args.max_results - role_found
        if limit <= 0 or STATE["search_blocked"]:
            continue
        try:
            found, total = paged_search("search_people", filters, limit)
        except SearchLimitReached as e:
            STATE["search_blocked"] = str(e)
            print(f"   Search allowance used up ({e}).")
            break
        if group == "role":
            role_found += len(found)
        totals[group] = totals.get(group, 0) + total
        people += [{**p, "_group": group} for p in found]
    return people, totals


def dedupe_people(people):
    """MoltSets sometimes holds two profiles for one person, and the founder search can return someone the
    role search already found. Keep one per LinkedIn URL / name, preferring the profile with an email."""
    kept, position = [], {}
    for p in people:
        keys = [k for k in (normalize_linkedin(p.get("linkedin_url")), (p.get("full_name") or "").strip().lower()) if k]
        hit = next((position[k] for k in keys if k in position), None)
        if hit is not None:
            if p.get("business_email") and not kept[hit].get("business_email"):
                kept[hit] = {**p, "_group": kept[hit]["_group"]}
            print(f"   Duplicate profile for {p.get('full_name')} merged.")
            for k in keys:
                position.setdefault(k, hit)
            continue
        for k in keys:
            position[k] = len(kept)
        kept.append(p)
    return kept


def diagnose_empty(company):
    """No one found: tell "company not in MoltSets" apart from "no matching roles".

    One company lookup by domain — free if the company isn't there, 1 search result if it is.
    """
    if STATE["search_blocked"]:
        return "search allowance used up — can't check whether the company is in MoltSets"
    try:
        status, res = call("search_companies", {"domain": company["domain"], "limit": 1})
    except (SearchLimitReached, ToolError) as e:
        return f"couldn't check whether the company is in MoltSets ({e})"
    found = (res.get("results") or []) if status == "ok" else []
    if not found:
        return (f"{company['domain']} isn't in MoltSets — check the website (another domain?) "
                "or use another provider")
    c = found[0]
    size = f"{c.get('employee_range') or '?'} employees"
    return (f"{c.get('name') or company['name']} is in MoltSets ({size}, {c.get('country') or 'country unknown'}) "
            "but nobody matches these roles — try --departments all, --seniority all, or --founders")


def prospect_row(company, p, rank, args, titles, wanted_levels, everyone):
    notes = []
    group = p["_group"]
    fit, fit_reason = role_fit(p, FOUNDER_TITLES if group == "founder" else titles, group)
    confidence, confidence_reason = match_confidence(p, company, everyone)
    for reason in (fit_reason, confidence_reason):
        if reason:
            notes.append(reason)
    usable = fit == "strong" and confidence != "low"  # only spend tokens on people worth contacting

    linkedin = normalize_linkedin(p.get("linkedin_url"))
    email = None
    if p.get("business_email"):
        email = email_record(p["business_email"], p.get("business_email_risk_score"),
                             p.get("business_email_validated_at"), "business", "people search")
    elif linkedin and usable and not args.no_email_fallback:
        status, res = try_call("linkedin_to_best_email", {"linkedin_url": linkedin}, notes)
        if status == "ok" and res.get("email"):
            email = email_record(res["email"], res.get("risk_score"), res.get("last_validated_at"),
                                 res.get("type"), "best-email lookup")
    email, email_note = assess_email(email, company["domain"], company.get("email_domains", ()))
    if email_note:
        notes.append(email_note)
    phone = lookup_mobile(linkedin, notes) if usable else {}
    department = p.get("functional_area")
    return {
        "target_company": company["name"],
        "target_domain": company["domain"],
        "rank": rank,
        "contact_type": "founder" if group == "founder" or FOUNDER_RE.search(p.get("title") or "") else "role",
        "role_fit": fit,
        "match_confidence": confidence,
        "full_name": p.get("full_name"),
        "job_title": p.get("title"),
        "seniority": p.get("seniority"),
        "department": ", ".join(department) if isinstance(department, list) else department,
        "company": (p.get("company") or {}).get("name"),
        "city": p.get("city"),
        "state": p.get("state"),
        "country": p.get("country"),
        "linkedin_url": linkedin,
        **(email or EMPTY_EMAIL),
        "mobile_phone": phone.get("mobile_phone"),
        "mobile_phone_validated_at": phone.get("last_validated_at"),
        "role_start_date": p.get("current_role_start_date"),
        "moltsets_score": p.get("_score"),
        "notes": "; ".join(notes),
    }


def print_prospect_row(row, phones):
    flags = [f for f in (row["role_fit"] == "weak" and "weak role match",
                         row["match_confidence"] != "high" and f"{row['match_confidence']} match confidence") if f]
    print(f"  {row['rank']}. {row['full_name']} — {row['job_title'] or '?'} ({row['seniority'] or 'level unknown'}), "
          f"{row['city'] or ''} {row['country'] or ''}" + (f"  [{'; '.join(flags)}]" if flags else ""))
    grade = f"  [{row['email_risk_score']}: {row['email_action']}]" if row["email"] else ""
    print(f"     {row['email'] or '(no email found)'}{grade}  {row['linkedin_url'] or ''}")
    if row["notes"]:
        print(f"     Notes: {row['notes']}")
    if phones:
        print(f"     Mobile: {row['mobile_phone'] or '(none found)'}")


def lead_key(row):
    """Same person = same LinkedIn URL, or same name at the same company."""
    linkedin = normalize_linkedin(row.get("linkedin_url"))
    if linkedin:
        return linkedin
    name = (row.get("full_name") or "").strip().lower()
    return ((row.get("target_domain") or "").lower(), name) if name else None


def prospect_company(company, titles, wanted_levels, args):
    """Find, check and rank the contacts at one company, printing each one. Used by prospect and visitors.

    Sets company["leads"] (the lead rows) and company["status"], and returns the leads. args needs max_results,
    founders, no_email_fallback and phones. StopRun and KeyboardInterrupt propagate, with company["leads"]
    holding what was found so far; a failed search becomes the status instead.
    """
    company["leads"] = []
    company_rows = company["leads"]
    try:
        people, totals = search_company_people(company, titles, wanted_levels, args)
        if not people and STATE["search_blocked"]:
            company["status"] = "not searched (search allowance used up)"
            return company_rows
        print(f"   {plural(len(people), 'person')} returned (total matching: "
              + ", ".join(f"{g} {t}" for g, t in totals.items()) + ")"
              + (f" in {company['countries']}" if company["countries"] else ""))
        if not people:
            reason = diagnose_empty(company)
            print(f"   No matches: {reason}.")
            company["status"] = f"no contacts: {reason}"
            return company_rows
        people = dedupe_people(people)
        # A company can email from another domain than its website (acme.example vs
        # acme-aero.example). Two or more of its people sharing that domain makes it the company's.
        alt = {}
        for p in people:
            d = email_domain(p.get("business_email"))
            if d and not is_personal_domain(d) and not is_academic_domain(d) \
                    and domain_stem(d) != domain_stem(company["domain"]):
                alt[d] = alt.get(d, 0) + 1
        company["email_domains"] = {d for d, n in alt.items() if n >= 2}
        if company["email_domains"]:
            print(f"   Also emails from: {', '.join(sorted(company['email_domains']))}")
        for p in people:
            p["_fit"] = role_fit(p, FOUNDER_TITLES if p["_group"] == "founder" else titles, p["_group"])[0]
            p["_confidence"] = match_confidence(p, company, people)[0]
        people.sort(key=lambda p: (
            CONFIDENCE_ORDER[p["_confidence"]], p["_fit"] == "weak", p["_group"] != "founder",
            relevance_key(p, FOUNDER_TITLES if p["_group"] == "founder" else titles,
                          [] if p["_group"] == "founder" else wanted_levels),
        ))
        for rank, p in enumerate(people, start=1):
            row = prospect_row(company, p, rank, args, titles, wanted_levels, people)
            company_rows.append(row)
            print_prospect_row(row, args.phones)
    except ToolError as e:
        print(f"   Search failed: {e}")
        company["status"] = f"search failed: {e}"
        return company_rows
    with_email = sum(1 for r in company_rows if r["email"])
    flagged = sum(1 for r in company_rows if r["role_fit"] == "weak" or r["match_confidence"] == "low")
    company["status"] = (f"{plural(len(company_rows), 'contact')} found, {with_email} with email"
                         + (f", {flagged} flagged" if flagged else ""))
    return company_rows


def profile_from_email_lookup(res):
    """Map a reverse_email_lookup result onto reverse_linkedin_lookup's field names."""
    return {
        "full_name": f"{res.get('first_name') or ''} {res.get('last_name') or ''}".strip(),
        "title": res.get("title"),
        "seniority": res.get("seniority"),
        "functional_area": res.get("functional_area"),
        "country": res.get("country"),
        "linkedin_url": res.get("linkedinurl"),
        "company": {
            "name": res.get("current_company"),
            "website_url": res.get("current_company_url"),
            "industry": res.get("current_industry"),
            "employee_range": res.get("company_employee_range"),
        },
        "business_email": res.get("work_email_confirmed"),
        "business_email_risk_score": res.get("work_email_confirmed_risk_score"),
        "business_email_validated_at": res.get("work_email_confirmed_status"),
    }
