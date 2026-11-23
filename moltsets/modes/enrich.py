"""enrich mode: contact details for people you already know."""

import csv
import os
import re

from moltsets.config import (
    COMPANY_COLUMNS,
    DOMAIN_COLUMNS,
    EMAIL_COLUMNS,
    FIRST_NAME_COLUMNS,
    LAST_NAME_COLUMNS,
    LINKEDIN_COLUMNS,
    NAME_COLUMNS,
)
from moltsets.util import (
    assess_email,
    domain_from_url,
    email_domain,
    email_record,
    EMPTY_EMAIL,
    is_personal_domain,
    normalize_linkedin,
    plural,
    slug,
    split_emails,
    valid_domain,
)
from moltsets.api import (
    check_budget,
    describe_search_allowance,
    lookup_mobile,
    remind_if_search_exhausted,
    SearchLimitReached,
    STATE,
    StopRun,
    try_call,
)
from moltsets.csvio import cell, column, csv_error, grade_summary, output_path, precheck, read_csv, write_csv
from moltsets.people import profile_from_email_lookup


def validate_prospect(label, name, company, domain_value, linkedin_value, email_value):
    """Turn one raw input row into a prospect. Returns (prospect or None, issue or None).

    A row needs a LinkedIn URL, an email, or a name + company domain. Lookups prefer
    LinkedIn (most complete), then email (exact, uses no search allowance), then name.
    """
    linkedin = normalize_linkedin(linkedin_value) if linkedin_value else None
    emails = split_emails(email_value)
    domain = valid_domain(domain_value) or (valid_domain(company) if "." in (company or "") else "")
    if not domain and emails and not is_personal_domain(email_domain(emails[0])):
        domain = email_domain(emails[0])
    issue = None
    if linkedin_value and not linkedin:
        issue = f"{label}: {linkedin_value!r} isn't a LinkedIn profile URL — it's ignored."
    if email_value and not emails:
        issue = f"{label}: {email_value!r} isn't an email address — it's ignored."
    if not (linkedin or emails):
        if not name:
            return None, issue or f"{label}: no LinkedIn URL, email or name — row will be skipped."
        if not domain:
            hint = f" ({company!r} is a company name; MoltSets needs its website domain)" if company else ""
            bad = f" ({domain_value!r} isn't a valid domain)" if domain_value else ""
            return None, f"{label}: no LinkedIn URL or email, and no company domain{hint or bad} — row will be skipped."
    prospect = {"name": name, "company": company, "domain": domain, "linkedin": linkedin,
                "email": emails[0] if emails else ""}
    return prospect, issue


def load_prospects(args, path):
    entries = []  # (where, name, company, domain, linkedin, email)
    if path:
        try:
            _, rows, columns = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            return [], [csv_error(path, e)], True
        name_col = column(columns, NAME_COLUMNS)
        first_col, last_col = column(columns, FIRST_NAME_COLUMNS), column(columns, LAST_NAME_COLUMNS)
        domain_col, company_col = column(columns, DOMAIN_COLUMNS), column(columns, COMPANY_COLUMNS)
        linkedin_col, email_col = column(columns, LINKEDIN_COLUMNS), column(columns, EMAIL_COLUMNS)
        has_name = name_col or (first_col and last_col)
        if not (linkedin_col or email_col or (has_name and (domain_col or company_col))):
            return [], [
                f"{path} needs a `linkedin_url` column, an `email` column, or `name` (or `first_name` + `last_name`) "
                f"plus a company domain column (`company_domain` / `domain` / `website`). Found: {list(columns.values())}."
            ], True
        for line_no, row in enumerate(rows, start=2):
            name = cell(row, name_col) or f"{cell(row, first_col)} {cell(row, last_col)}".strip()
            entries.append((f"line {line_no}", name, cell(row, company_col), cell(row, domain_col),
                            cell(row, linkedin_col), cell(row, email_col)))
    if args.name or args.company or args.linkedin or args.email:
        entries.append(("command line", args.name or "", "", args.company or "", args.linkedin or "", args.email or ""))

    prospects, issues, seen = [], [], {}
    for where, name, company, domain_value, linkedin_value, email_value in entries:
        if not any((name, company, domain_value, linkedin_value, email_value)):
            continue
        label = f"{where} ({name})" if name else where
        prospect, issue = validate_prospect(label, name, company, domain_value, linkedin_value, email_value)
        if issue:
            issues.append(issue)
        if not prospect:
            continue
        key = prospect["linkedin"] or prospect["email"].lower() or (prospect["name"].lower(), prospect["domain"])
        if key in seen:
            issues.append(f"{label}: duplicate of {seen[key]} — skipped (saves a lookup).")
            continue
        seen[key] = where
        prospects.append(prospect)
    if not prospects:
        issues.append("No valid prospects left to look up.")
    return prospects, issues, not prospects


def enrich_prospect(prospect):
    """Run the lookup chain for one prospect and return its CSV row."""
    notes = []
    linkedin = prospect["linkedin"]
    email = None
    basic = {}  # search_business_profile_by_name result
    profile = {}  # reverse_linkedin_lookup / reverse_email_lookup result

    if not linkedin and prospect["email"]:
        status, res = try_call("reverse_email_lookup", {"email": prospect["email"]}, notes)
        if status == "ok":
            profile = profile_from_email_lookup(res)
            linkedin = normalize_linkedin(profile.get("linkedin_url"))
            if profile["business_email"] and profile["business_email"].lower() != prospect["email"].lower():
                notes.append(f"MoltSets' work email differs from the input ({prospect['email']})")
        else:
            notes.append("input email not found in MoltSets")

    if not linkedin and not profile and prospect["name"] and prospect["domain"]:
        if STATE["search_blocked"]:
            notes.append("name lookup skipped: MoltSets search allowance used up")
        else:
            payload = {"name": prospect["name"], "company": prospect["domain"]}
            try:
                # Free count first: skips the paid lookups when nobody matches, flags ambiguous names.
                status, res = try_call("search_linkedin_profile", {**payload, "count_only": True}, notes)
                count = res.get("count") if status in ("ok", "not_found") else None
                if count and count > 1:
                    notes.append(f"{count} LinkedIn profiles match this name at {prospect['domain']} — check it's the right person")
                if count != 0:
                    status, res = try_call("search_business_email_by_name", payload, notes)
                    if status == "ok" and res.get("email"):
                        email = email_record(res["email"], res.get("risk_score"), res.get("last_validated_at"),
                                             "business", "name + company")
                    linkedin = normalize_linkedin(res.get("linkedin_url"))
                    if not linkedin:
                        status, res = try_call("search_business_profile_by_name", payload, notes)
                        if status == "ok":
                            basic = res
                            linkedin = normalize_linkedin(res.get("linkedin_url"))
            except SearchLimitReached as e:
                STATE["search_blocked"] = str(e)
                print(f"   MoltSets search allowance used up — name lookups are skipped from here on ({e}).")
                notes.append("name lookup skipped: MoltSets search allowance used up")

    if linkedin and not profile:
        status, res = try_call("reverse_linkedin_lookup", {"linkedin_url": linkedin}, notes)
        if status == "ok":
            profile = res
    if not email and profile.get("business_email"):
        email = email_record(profile["business_email"], profile.get("business_email_risk_score"),
                             profile.get("business_email_validated_at"), "business",
                             "email lookup" if prospect["email"] and not prospect["linkedin"] else "LinkedIn profile")
    if not email and linkedin:
        status, res = try_call("linkedin_to_best_email", {"linkedin_url": linkedin}, notes)
        if status == "ok" and res.get("email"):
            email = email_record(res["email"], res.get("risk_score"), res.get("last_validated_at"),
                                 res.get("type"), "best-email lookup")
    company_domain = domain_from_url((profile.get("company") or {}).get("website_url")) or prospect["domain"]
    email, email_note = assess_email(email, company_domain)
    if email_note:
        notes.append(email_note)
    phone = lookup_mobile(linkedin, notes)

    company = profile.get("company") or {}
    firmographics = basic.get("firmographics") or {}
    department = profile.get("functional_area")
    personal_email = profile.get("personal_email")
    company_site = company.get("website_url")

    if email:
        lookup_status = "email found"
    elif profile or basic:
        lookup_status = "profile found, no email"
    elif any(n.startswith("name lookup skipped") for n in notes):
        lookup_status = "not looked up"
    elif any(re.match(r"^[a-z_]+: ", n) for n in notes):  # a ToolError ("tool: code (message)")
        lookup_status = "error"
    else:
        lookup_status = "not found"

    return {
        "input_name": prospect["name"],
        "input_company": prospect["company"] or prospect["domain"],
        "input_linkedin": prospect["linkedin"],
        "input_email": prospect["email"],
        "full_name": profile.get("full_name") or basic.get("full_name") or prospect["name"],
        "job_title": profile.get("title") or basic.get("title"),
        "seniority": profile.get("seniority"),
        "department": ", ".join(department) if isinstance(department, list) else department,
        "company": company.get("name") or basic.get("company") or prospect["company"],
        "company_domain": domain_from_url(company_site) if company_site else basic.get("company_domain") or prospect["domain"],
        "company_industry": company.get("industry") or firmographics.get("industry"),
        "company_size": company.get("employee_range") or firmographics.get("employee_count"),
        "city": profile.get("city"),
        "state": profile.get("state"),
        "country": profile.get("country"),
        "linkedin_url": linkedin,
        **(email or EMPTY_EMAIL),
        "personal_email": personal_email if personal_email and personal_email != (email or {}).get("email") else None,
        "personal_email_risk_score": profile.get("personal_email_risk_score") if personal_email else None,
        "mobile_phone": phone.get("mobile_phone"),
        "mobile_phone_validated_at": phone.get("last_validated_at"),
        "lookup_status": lookup_status,
        "notes": "; ".join(notes),
    }


def print_enriched(row):
    company = f", {row['company']}" if row["company"] else ""
    print(f"   Name:     {row['full_name']} — {row['job_title'] or '(title unknown)'}{company}")
    if row["email"]:
        validated = f", validated {row['email_validated_at']}" if row["email_validated_at"] else ""
        print(f"   Email:    {row['email']}  [{row['email_risk_score']}: {row['email_action']}; "
              f"{row['email_type'] or 'business'}{validated}]")
    else:
        print("   Email:    (none found)")
    if row["personal_email"]:
        print(f"   Personal: {row['personal_email']}  [{row['personal_email_risk_score'] or 'F'}]")
    if STATE["phones"] or row["mobile_phone"]:
        print(f"   Mobile:   {row['mobile_phone'] or '(none found)'}")
    print(f"   LinkedIn: {row['linkedin_url'] or '(not found)'}")
    if row["notes"]:
        print(f"   Notes:    {row['notes']}")


def run_enrich(args):
    STATE["phones"] = args.phones

    def load(path):
        prospects, issues, blocking = load_prospects(args, path)
        if blocking:
            return prospects, issues, True
        by_name = sum(1 for p in prospects if not (p["linkedin"] or p["email"]))
        budget_issues, budget_blocking = check_budget(
            search_needed=by_name, tokens_needed=3 * len(prospects),  # only profile-by-name may use search results
            phones_wanted=len(prospects) if args.phones else 0,
        )
        return prospects, issues + budget_issues, budget_blocking

    prospects, args.prospects_file = precheck(load, args.prospects_file, "prospect")
    if args.output:
        output = args.output
    elif args.prospects_file:
        output = f"{slug(os.path.splitext(os.path.basename(args.prospects_file))[0])}_moltsets_contacts.csv"
    else:
        p = prospects[0]
        stem = p["name"] or p["email"].split("@")[0] or (p["linkedin"] or "prospect").rsplit("/", 1)[-1]
        output = f"{slug(stem)}_moltsets_contacts.csv"
    print(f"\nLooking up {plural(len(prospects), 'prospect')}{' (with mobile phones)' if STATE['phones'] else ''} ...")

    rows = []
    for i, prospect in enumerate(prospects, start=1):
        who = prospect["name"] or prospect["email"] or prospect["linkedin"]
        print(f"\n[{i}/{len(prospects)}] {who}" + (f" @ {prospect['domain']}" if prospect["domain"] else ""))
        try:
            row = enrich_prospect(prospect)
        except (StopRun, KeyboardInterrupt) as e:
            reason = "interrupted" if isinstance(e, KeyboardInterrupt) else str(e).rstrip(".")
            print(f"\n   Stopping: {reason}. Saving results so far; the rest are marked 'not processed'.")
            rows += [{"input_name": p["name"], "input_company": p["company"] or p["domain"],
                      "input_linkedin": p["linkedin"], "input_email": p["email"],
                      "lookup_status": "not processed", "notes": f"run stopped: {reason}"} for p in prospects[i - 1:]]
            break
        print_enriched(row)
        rows.append(row)

    done = [r for r in rows if r["lookup_status"] != "not processed"]
    print("\n=== Summary ===")
    print(f"  Looked up: {len(done)}/{len(prospects)}")
    print(f"  Emails found: {sum(1 for r in done if r.get('email'))} (grades {grade_summary(done)})")
    if any(r.get("email_risk_score") == "D" for r in done):
        print("  !! Some emails are graded D (spam trap / complaint) — marked 'do not send'.")
    if args.phones:
        print(f"  Mobile numbers found: {sum(1 for r in done if r.get('mobile_phone'))}")
    print(f"  Search results left: {describe_search_allowance()}")
    remind_if_search_exhausted()
    output = output_path(output)
    write_csv(output, rows)
    print(f"\nSaved {plural(len(rows), 'row')} to {output}")
