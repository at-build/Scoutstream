"""verify mode: grade emails and catch job changes in existing lead files."""

import csv
import os

from moltsets.config import (
    COMPANY_COLUMNS,
    DOMAIN_COLUMNS,
    EMAIL_COLUMNS,
    LINKEDIN_COLUMNS,
    NAME_COLUMNS,
    PROVIDERS,
    RISK_ACTIONS,
)
from moltsets.util import (
    domain_from_url,
    domain_stem,
    email_domain,
    is_personal_domain,
    normalize_linkedin,
    plural,
    same_company,
    slug,
    split_emails,
    titles_differ,
    valid_domain,
)
from moltsets.api import check_budget, STATE, StopRun, try_call
from moltsets.csvio import cell, column, csv_error, output_path, precheck, read_csv, write_csv
from moltsets.people import profile_from_email_lookup


def provider_of(path):
    name = os.path.basename(path).lower()
    return next((p for p in PROVIDERS if p in name), os.path.splitext(os.path.basename(path))[0])


def load_lead_files(args):
    """Read every lead file. Returns (files, issues, blocking); files hold rows + detected columns."""
    files, issues = [], []
    for path in args.files:
        try:
            fieldnames, rows, columns = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            issues.append(csv_error(path, e) + " File skipped.")
            continue
        if args.limit:
            rows = rows[: args.limit]
        cols = {
            "email": column(columns, EMAIL_COLUMNS),
            "linkedin": column(columns, LINKEDIN_COLUMNS),
            "company": column(columns, COMPANY_COLUMNS),
            "domain": column(columns, DOMAIN_COLUMNS),
            "title": column(columns, ("job_title", "title")),
            "name": column(columns, NAME_COLUMNS),
        }
        if not (cols["email"] or cols["linkedin"]):
            issues.append(f"{path}: no email or LinkedIn column (found {fieldnames}) — file skipped.")
            continue
        checkable = [r for r in rows if split_emails(cell(r, cols["email"])) or normalize_linkedin(cell(r, cols["linkedin"]))]
        if len(checkable) < len(rows):
            issues.append(f"{path}: {len(rows) - len(checkable)} of {len(rows)} rows have no email or LinkedIn URL "
                          "— they're kept in the output but not checked.")
        if not (cols["company"] or cols["domain"] or args.expected_company):
            no_work_email = sum(1 for r in checkable if not any(
                not is_personal_domain(email_domain(e)) for e in split_emails(cell(r, cols["email"]))))
            if no_work_email:
                issues.append(
                    f"{path}: no company column, so for {no_work_email} row(s) without a work email the job-change "
                    "check can't tell which employer to expect. Pass --expected-company (e.g. exampleelectric.example) to enable it."
                )
        files.append({"path": path, "provider": provider_of(path), "fieldnames": fieldnames,
                      "rows": rows, "cols": cols})
    if not files:
        issues.append("No readable lead files left to verify.")
    return files, issues, not files


def lookup_cached(cache, tool, payload, notes):
    key = (tool, tuple(sorted(payload.items())))
    if key not in cache:
        cache[key] = try_call(tool, payload, notes)
    return cache[key]


def verify_row(row, cols, args, cache):
    """Job-change check + email grading for one lead. Returns the ms_* columns to append."""
    notes = []
    emails = split_emails(cell(row, cols["email"]))[:3]
    linkedin = normalize_linkedin(cell(row, cols["linkedin"]))
    if not (emails or linkedin):
        return {"ms_status": "not checked", "ms_notes": "no email or LinkedIn URL"}

    expected_name = cell(row, cols["company"]) or (args.expected_company if "." not in (args.expected_company or "") else "")
    expected_domain = valid_domain(cell(row, cols["domain"])) or (
        valid_domain(args.expected_company) if "." in (args.expected_company or "") else "")
    work_domains = [email_domain(e) for e in emails if not is_personal_domain(email_domain(e))]
    expected_domain = expected_domain or (work_domains[0] if work_domains else "")
    expected_name = expected_name or domain_stem(expected_domain)

    profile, graded = {}, []  # graded: (input email, grade, validated_at) for each input email MoltSets knows
    for e in emails:
        status, res = lookup_cached(cache, "reverse_email_lookup", {"email": e}, notes)
        if status != "ok":
            continue
        p = profile_from_email_lookup(res)
        profile = profile or p
        if (p["business_email"] or "").lower() == e.lower():
            graded.append((e, p["business_email_risk_score"] or "F", p["business_email_validated_at"]))
        else:
            for pe, grade in zip(res.get("personal_emails") or [], res.get("personal_emails_risk_score") or []):
                if pe.lower() == e.lower():
                    graded.append((e, grade or "F", None))
    if not profile and linkedin:
        status, res = lookup_cached(cache, "reverse_linkedin_lookup", {"linkedin_url": linkedin}, notes)
        if status == "ok":
            profile = res

    company = profile.get("company") or {}
    ms_email = profile.get("business_email")
    current_name = company.get("name")
    current_domain = domain_from_url(company.get("website_url")) or (
        email_domain(ms_email) if not is_personal_domain(email_domain(ms_email)) else "")
    same = same_company(expected_name, expected_domain, current_name, current_domain) if profile else None
    input_title = cell(row, cols["title"])
    current_title = profile.get("title")
    title_changed = titles_differ(input_title, current_title)

    ms_email_new = ms_email and ms_email.lower() not in {e.lower() for e in emails}
    # MoltSets' own data can lag: a work email at a different company than the profile's employer is likely stale.
    email_mismatch = bool(ms_email and current_domain and not is_personal_domain(email_domain(ms_email))
                          and domain_stem(email_domain(ms_email)) != domain_stem(current_domain))
    if email_mismatch:
        notes.append(f"MoltSets' work email {ms_email} doesn't match their current employer ({current_domain}) "
                     "— likely outdated, don't use it")
    best = min(graded, key=lambda g: "ADBCF".index(g[1]) if g[1] in "ADBCF" else 9) if graded else None
    worst_d = any(g[1] == "D" for g in graded)

    if not profile:
        status = "not found in MoltSets"
    elif same is False:
        status = "moved company"
    elif worst_d:
        status = "email: do not send (D)"
    elif emails and not graded:
        status = "email not confirmed" if not ms_email_new or email_mismatch else "different email on file"
    elif not emails and ms_email_new and email_mismatch:
        status = "email doesn't match employer"
    elif not emails and ms_email_new:
        status = "new email found"
    elif title_changed:
        status = "title changed"
    else:
        status = "ok"
    if emails and not graded and profile:
        notes.append("MoltSets doesn't have the input email on file for this person")

    return {
        "ms_status": status,
        "ms_still_at_company": {True: "yes", False: "no", None: "unknown"}[same],
        "ms_current_company": current_name,
        "ms_current_domain": current_domain,
        "ms_current_title": current_title,
        "ms_title_changed": "yes" if title_changed else ("no" if input_title and current_title else ""),
        "ms_role_start_date": profile.get("current_role_start_date"),
        "ms_input_email_grade": best[1] if best else "",
        "ms_input_email_action": RISK_ACTIONS.get(best[1], "") if best else "",
        "ms_input_email_validated_at": best[2] if best else "",
        "ms_work_email": ms_email if ms_email_new else "",
        "ms_work_email_grade": profile.get("business_email_risk_score") if ms_email_new else "",
        "ms_personal_email": profile.get("personal_email"),
        "ms_linkedin_url": profile.get("linkedin_url") or linkedin,
        "ms_notes": "; ".join(notes),
    }


def run_verify(args):
    def load(_):
        files, issues, blocking = load_lead_files(args)
        if blocking:
            return files, issues, True
        lookups = set()
        for f in files:
            for r in f["rows"]:
                emails = split_emails(cell(r, f["cols"]["email"]))[:3]
                lookups |= {e.lower() for e in emails} or ({normalize_linkedin(cell(r, f["cols"]["linkedin"]))} - {None})
        for f in files:
            print(f"   {f['path']}: {plural(len(f['rows']), 'row')} ({f['provider']})")
        print(f"   {plural(len(lookups), 'unique lookup')} (each email / LinkedIn URL is looked up once, across files)")
        budget_issues, budget_blocking = check_budget(tokens_needed=len(lookups))
        return files, issues + budget_issues, budget_blocking

    files, _ = precheck(load, None, "file")
    cache, comparison = {}, []
    stopped = None
    for f in files:
        print(f"\n##### {f['path']} ({f['provider']}) #####")
        out_rows = []
        for i, row in enumerate(f["rows"], start=1):
            if stopped:
                out_rows.append({**row, "ms_status": "not processed", "ms_notes": f"run stopped: {stopped}"})
                continue
            try:
                result = verify_row(row, f["cols"], args, cache)
            except (StopRun, KeyboardInterrupt) as e:
                stopped = "interrupted" if isinstance(e, KeyboardInterrupt) else str(e).rstrip(".")
                print(f"   Stopping: {stopped}. Saving results so far.")
                out_rows.append({**row, "ms_status": "not processed", "ms_notes": f"run stopped: {stopped}"})
                continue
            out_rows.append({**row, **result})
            name = cell(row, f["cols"]["name"]) or cell(row, f["cols"]["email"]) or cell(row, f["cols"]["linkedin"])
            extra = ""
            if result["ms_status"] == "moved company":
                extra = f" -> now {result['ms_current_title'] or '?'} at {result['ms_current_company']}"
            elif result.get("ms_input_email_grade"):
                extra = f" (email {result['ms_input_email_grade']})"
            elif result.get("ms_work_email"):
                extra = f" (MoltSets email: {result['ms_work_email']} [{result['ms_work_email_grade'] or 'F'}])"
            print(f"  {i}. {name}: {result['ms_status']}{extra}")

        output = output_path(os.path.join(
            args.output_dir or ".", f"{slug(os.path.splitext(os.path.basename(f['path']))[0])}_moltsets_verified.csv"))
        write_csv(output, out_rows)
        print(f"   Saved {output}")

        checked = [r for r in out_rows if r.get("ms_status") not in ("not checked", "not processed")]
        with_email = [r for r in out_rows if split_emails(cell(r, f["cols"]["email"]))]
        graded = [r for r in with_email if r.get("ms_input_email_grade")]
        count = lambda pred, rows=checked: sum(1 for r in rows if pred(r))  # noqa: E731
        comparison.append({
            "provider": f["provider"],
            "file": f["path"],
            "leads": len(out_rows),
            "leads_with_email": len(with_email),
            "emails_graded": len(graded),
            "grade_A": sum(1 for r in graded if r["ms_input_email_grade"] == "A"),
            "grade_B": sum(1 for r in graded if r["ms_input_email_grade"] == "B"),
            "grade_C": sum(1 for r in graded if r["ms_input_email_grade"] == "C"),
            "grade_D": sum(1 for r in graded if r["ms_input_email_grade"] == "D"),
            "grade_F": sum(1 for r in graded if r["ms_input_email_grade"] == "F"),
            "pct_sendable_A_B": f"{100 * sum(1 for r in graded if r['ms_input_email_grade'] in 'AB') // len(with_email)}%"
            if with_email else "",
            "new_emails_from_moltsets": count(
                lambda r: r.get("ms_work_email") and r["ms_status"] != "email doesn't match employer"),
            "moved_company": count(lambda r: r["ms_status"] == "moved company"),
            "title_changed": count(lambda r: r.get("ms_title_changed") == "yes"),
            "not_in_moltsets": count(lambda r: r["ms_status"] == "not found in MoltSets"),
        })

    print("\n=== Provider comparison ===")
    headers = ["provider", "leads", "leads_with_email", "emails_graded", "grade_A", "grade_B", "grade_C", "grade_D",
               "grade_F", "pct_sendable_A_B", "new_emails_from_moltsets", "moved_company", "title_changed",
               "not_in_moltsets"]
    widths = [max(len(h), *(len(str(c[h])) for c in comparison)) for h in headers]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for c in comparison:
        print("  " + "  ".join(str(c[h]).ljust(w) for h, w in zip(headers, widths)))
    print("  (pct_sendable_A_B = provider emails graded A or B, out of leads that had an email)")
    if len(comparison) > 1:
        path = output_path(os.path.join(args.output_dir or ".", "moltsets_provider_comparison.csv"))
        write_csv(path, comparison)
        print(f"\nSaved comparison to {path}")
    tokens = STATE["last_metadata"].get("tokens_remaining")
    if tokens is not None:
        print(f"Tokens left: {'unlimited' if tokens == -1 else tokens}")
