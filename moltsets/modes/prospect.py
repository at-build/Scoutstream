"""prospect mode: decision makers at given companies."""

import csv
import os

from moltsets.config import (
    COMPANY_COLUMNS,
    DOMAIN_COLUMNS,
    LOCATION_COLUMNS,
    SENIORITY_LADDER,
    SENIORITY_TERMS,
)
from moltsets.util import (
    normalize_country,
    plural,
    slug,
    split_terms,
    valid_domain,
)
from moltsets.api import check_budget, describe_search_allowance, remind_if_search_exhausted, STATE, StopRun
from moltsets.csvio import (
    cell,
    column,
    csv_error,
    grade_summary,
    merge_rows,
    output_path,
    precheck,
    read_csv,
    write_csv,
)
from moltsets.safety import write_table
from moltsets.people import (
    contact_columns,
    CONTACT_PREFIX,
    lead_key,
    prospect_company,
    resolve_seniority,
    resolve_titles,
    SEARCH_STATUS_COLUMN,
)


WITH_CONTACTS_SUFFIX = "_with_contacts"


def load_companies(args, path, table=None):
    """Companies to prospect from the CSV and/or --company/--website. Returns (companies, issues, blocking).

    With a CSV, `table` (a dict) is filled with what's needed to write the enriched copy: the original
    columns, every non-blank row with its line number, and why any row won't be searched.
    """
    table = {} if table is None else table
    table.clear()
    entries = []  # (where, line, name, website, location_terms)
    issues = []
    if path:
        try:
            fieldnames, rows, columns = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            return [], [csv_error(path, e)], True
        if SEARCH_STATUS_COLUMN in fieldnames:
            # An earlier _with_contacts output: drop its contact columns and the repeated company rows.
            fieldnames = [f for f in fieldnames if not f.startswith(CONTACT_PREFIX)]
            unique, seen_rows = [], set()
            for row in rows:
                values = tuple((row.get(f) or "") for f in fieldnames)
                if values not in seen_rows:
                    seen_rows.add(values)
                    unique.append({f: row.get(f) for f in fieldnames})
            rows = unique
            columns = {k: v for k, v in columns.items() if not v.startswith(CONTACT_PREFIX)}
            issues.append(f"{path} is an earlier results file: its contact_ columns are dropped and each company is "
                          "searched again (lookups made in the last 30 days come from the cache, free).")
        company_col, domain_col = column(columns, COMPANY_COLUMNS), column(columns, DOMAIN_COLUMNS)
        # A `country` column is often the company's HQ, not where its people are: with
        # --ignore-location-column it isn't a filter, but is still used to judge match confidence.
        location_col = column(columns, LOCATION_COLUMNS)
        if not domain_col:
            return [], [
                f"{path} needs a website/domain column (`website`, `domain` or `company_domain`); "
                f"found {list(columns.values())}."
            ], True
        table.update(fieldnames=fieldnames, rows=[], status={})
        for line_no, row in enumerate(rows, start=2):
            if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                continue  # blank row
            table["rows"].append((line_no, row))
            entries.append((f"line {line_no}", line_no, cell(row, company_col), cell(row, domain_col),
                            split_terms(cell(row, location_col), ";")))
    entries += [("--company", None, name, website, []) for name, website in zip(args.company, args.website)]

    companies, seen = [], {}
    for where, line, name, website, location_terms in entries:
        if not (name or website):
            if line:
                table["status"][line] = "skipped: no company name or website"
            continue
        label = f"{where} ({name})" if name else where
        domain = valid_domain(website)
        if not domain:
            reason = f"website {website!r} isn't a valid domain" if website else "missing website"
            issues.append(f"{label}: {reason} — company will be skipped.")
            if line:
                table["status"][line] = f"skipped: {reason}"
            continue
        if domain in seen:
            issues.append(f"{label}: same website as {seen[domain][0]} — skipped.")
            if line:
                table["status"][line] = f"skipped: same website as {seen[domain][0]}, whose rows list the contacts"
            continue
        seen[domain] = (label, line)
        place_terms = [t.strip() for term in location_terms for t in term.split("/") if t.strip()]
        hq = [normalize_country(t) for t in place_terms] if args.ignore_location_column else []
        countries = ([] if args.ignore_location_column else [normalize_country(t) for t in location_terms]) \
            or [normalize_country(c) for c in args.location]
        companies.append({"name": name or domain, "domain": domain, "countries": countries, "line": line,
                          "hq_countries": hq})
    if not companies:
        issues.append("No valid companies left to search.")
    return companies, issues, not companies


def with_contacts_path(input_path):
    stem, ext = os.path.splitext(input_path)
    stem = stem.removesuffix(WITH_CONTACTS_SUFFIX)
    return f"{stem}{WITH_CONTACTS_SUFFIX}{ext or '.csv'}"


def write_with_contacts(path, table, companies):
    """The input CSV with contact columns added: one row per contact (the company's columns repeated),
    and one row with a blank contact and a status for companies with none, so every input row is kept."""
    by_line = {c["line"]: c for c in companies if c["line"]}
    lead_fields = []
    for c in companies:
        for lead in c.get("leads", []):
            lead_fields += [k for k in contact_columns(lead) if k not in lead_fields]
    contact_fields = [SEARCH_STATUS_COLUMN] + lead_fields
    out = []

    def emit(base, company, status):
        leads = company.get("leads", []) if company else []
        for lead in leads:
            out.append({**base, SEARCH_STATUS_COLUMN: status, **contact_columns(lead)})
        if not leads:
            extra = {"contact_search_company": company["name"], "contact_search_domain": company["domain"]} \
                if company else {}
            out.append({**base, SEARCH_STATUS_COLUMN: status, **extra})

    for line, row in table["rows"]:
        base = {f: row.get(f, "") for f in table["fieldnames"]}
        company = by_line.get(line)
        emit(base, company, company["status"] if company else table["status"].get(line, "skipped"))
    for company in companies:  # --company / --website additions, after the file's rows
        if not company["line"]:
            emit({}, company, company["status"])

    fieldnames = table["fieldnames"] + [f for f in contact_fields if f not in table["fieldnames"]]
    for row in out:  # contact_search_company / _domain when no company in the run has a contact
        fieldnames += [k for k in row if k not in fieldnames]
    write_table(path, fieldnames, out)
    return len(out)


def run_prospect(args):
    titles, literal = resolve_titles(args.departments)
    wanted_levels, _, unmatched_seniority = resolve_seniority(args.seniority)
    STATE["phones"] = args.phones

    table = {}  # the input CSV, for the enriched copy

    def load(path):
        companies, issues, blocking = load_companies(args, path, table)
        if unmatched_seniority:
            issues.append(
                f"--seniority: {unmatched_seniority} matched no MoltSets level — continuing with "
                f"{wanted_levels or 'all levels'}. Known: {sorted(SENIORITY_TERMS)} + {list(SENIORITY_LADDER)} or 'all'."
            )
        if blocking:
            return companies, issues, True
        print(f"   Title keywords: {titles or '(any title)'}" + (f"  (used as typed: {literal})" if literal else ""))
        print(f"   Seniority: {wanted_levels or 'all levels'}"
              + ("  (then people with no seniority on record fill any free slots)" if wanted_levels else ""))
        if args.founders:
            print(f"   Founders: up to {args.founders} per company (separate search)")
        per_company = args.max_results + args.founders
        n = len(companies) * per_company
        budget_issues, budget_blocking = check_budget(
            search_needed=n + len(companies),  # + a company lookup when a company returns nobody
            tokens_needed=n * (1 if args.no_email_fallback else 2),
            phones_wanted=n if args.phones else 0, search_required=True,
        )
        return companies, issues + budget_issues, budget_blocking

    companies, args.companies_file = precheck(load, args.companies_file, "company")
    if args.companies_file:
        output = args.output or with_contacts_path(args.companies_file)
    elif args.output:
        output = args.output
    elif len(companies) == 1:
        output = f"{slug(companies[0]['name'])}_moltsets_leads.csv"
    elif args.companies_file:
        output = f"{slug(os.path.splitext(os.path.basename(args.companies_file))[0])}_moltsets_leads.csv"
    else:
        output = "moltsets_leads.csv"

    rows, stopped = [], None
    for ci, company in enumerate(companies, start=1):
        company["leads"] = []
        if stopped or STATE["search_blocked"]:
            company["status"] = "not searched (" + ("run stopped" if stopped else "search allowance used up") + ")"
            continue
        print(f"\n##### [{ci}/{len(companies)}] {company['name']} ({company['domain']}) #####")
        try:
            prospect_company(company, titles, wanted_levels, args)
        except (StopRun, KeyboardInterrupt) as e:
            stopped = "interrupted" if isinstance(e, KeyboardInterrupt) else str(e).rstrip(".")
            print(f"   Stopping: {stopped}. Saving results so far.")
            company["status"] = f"{plural(len(company['leads']), 'contact')} found before the run stopped ({stopped})"
        rows += company["leads"]
    summary = [(c["name"], c["status"]) for c in companies]

    print("\n=== Summary ===")
    for name, status in summary:
        print(f"  {name}: {status}")
    with_email = [r for r in rows if r["email"]]
    print(f"  Total: {plural(len(rows), 'lead')}, {len(with_email)} with email (grades {grade_summary(rows)})")
    if any(r["email_risk_score"] == "D" for r in rows):
        print("  !! Some emails are graded D (spam trap / complaint) — marked 'do not send'.")
    if any(str(r["email_action"]).startswith("verify first") for r in rows):
        print("  !! Some emails aren't on the company's domain (old employer / university) — marked 'verify first'.")
    print(f"  Search results left: {describe_search_allowance()}")
    remind_if_search_exhausted()
    if args.companies_file:
        output = output_path(output)
        n = write_with_contacts(output, table, companies)
        print(f"\nSaved {output}: your {plural(len(table['rows']), 'row')} with contact columns added "
              f"({plural(n, 'row')} — one per contact, one per company with none)")
        return
    if not rows:
        print("\nNo leads found; no CSV written.")
        return
    if args.append and os.path.exists(output):
        try:
            _, existing, _ = read_csv(output)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            print(f"   Can't read {output} to append to it ({e}).")
            output, existing = output_path(output, append_hint=True), []
        merged, added, replaced = merge_rows(existing, rows, lead_key)
        write_csv(output, merged)
        print(f"\nMerged into {output}: {added} new, {replaced} updated, {len(merged)} leads in total")
        return
    output = output_path(output, append_hint=True)
    write_csv(output, rows)
    print(f"\nSaved {plural(len(rows), 'lead')} to {output}")
