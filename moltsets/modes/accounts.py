"""accounts mode: build a target-company list from firmographic filters."""

import shlex

from moltsets.config import SEARCH_WINDOW
from moltsets.util import plural
from moltsets.api import check_budget, paged_search, remind_if_search_exhausted, SearchLimitReached, StopRun
from moltsets.csvio import load_exclusions, output_path, precheck, write_csv
from moltsets.companies import company_filters, company_row


def run_accounts(args):
    def load(_):
        filters, issues, blocking = company_filters(args)
        if blocking:
            return [], issues, True
        excluded, exclusion_issues = load_exclusions(args.exclude_file, args.exclude_domain)
        if excluded:
            filters["exclude_domain"] = excluded
        print(f"   Filters: { {k: v for k, v in filters.items() if k != 'exclude_domain'} }")
        print(f"   Excluding {plural(len(excluded), 'company')} you already have")
        budget_issues, budget_blocking = check_budget(
            search_needed=args.max_results, tokens_needed=args.max_results, search_required=True
        )
        return [filters], issues + exclusion_issues + budget_issues, budget_blocking

    (filters,), _ = precheck(load, None, "search")
    print(f"\nSearching companies (up to {args.max_results}) ...")
    try:
        companies, total = paged_search("search_companies", filters, args.max_results)
    except (SearchLimitReached, StopRun) as e:
        print(f"   Stopped: {e}")
        companies, total = [], 0
    print(f"   {plural(len(companies), 'company')} returned "
          f"(total matching: {total}{'+' if total >= SEARCH_WINDOW else ''}).")

    location = args.location if args.location is not None else "; ".join(filters.get("country", []))
    rows = []
    for c in companies:
        domain = c.get("domain") or ""
        rows.append(company_row(c, location))
        print(f"  - {c.get('name')} ({domain}) — {c.get('industry') or '?'}, {c.get('employee_range') or '?'} "
              f"employees, {c.get('revenue_range') or '?'}, {c.get('city') or ''} {c.get('country') or ''}")

    remind_if_search_exhausted()
    if not rows:
        print("\nNo companies found; no CSV written.")
        return
    output = output_path(args.output or "moltsets_accounts.csv")
    write_csv(output, rows)
    print(f"\nSaved {plural(len(rows), 'company')} to {output}")
    print(f"Next: python3 moltsets_poc.py prospect --companies-file {shlex.quote(output)}")
