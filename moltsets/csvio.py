"""Prompts, the pre-run check loop, and CSV reading / writing."""

import csv
import os
import re
import sys

from moltsets.config import DOMAIN_COLUMNS, EMAIL_COLUMNS
from moltsets.util import email_domain, is_personal_domain, plural, split_emails, split_terms, valid_domain
from moltsets.api import remind_if_search_exhausted
from moltsets.safety import clean_text, write_table


def output_path(path, append_hint=False):
    """Never overwrite an earlier result silently: pick path_2.csv, path_3.csv, ... instead."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    hint = "use --append to merge into it" if append_hint else "rename or delete it to reuse the name"
    print(f"   {path} already exists — saving to {stem}_{n}{ext} instead ({hint}).")
    return f"{stem}_{n}{ext}"


def merge_rows(existing, new, key):
    """Merge new rows into existing ones by key(row). A new row replaces an old one unless it lost the email."""
    merged, index = list(existing), {}
    for i, row in enumerate(merged):
        if key(row):
            index.setdefault(key(row), i)
    added = replaced = 0
    for row in new:
        k = key(row)
        if k and k in index:
            old = merged[index[k]]
            if row.get("email") or not old.get("email"):
                merged[index[k]] = row
                replaced += 1
            continue
        if k:
            index[k] = len(merged)
        merged.append(row)
        added += 1
    return merged, added, replaced


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:  # not running interactively (piped / scheduled)
        print()
        sys.exit("No answer available (non-interactive run); stopped before any MoltSets lookup.")


def report_issues(issues):
    print(f"\n!! Pre-run check found {len(issues)} issue(s):")
    for issue in issues:
        print(f"   - {issue}")


def wait_for_reupload(path):
    """Pause (no lookups run) until the user has fixed or replaced the input CSV."""
    print(f"\nNothing run yet. Fix and save {path}, or prepare a corrected CSV.")
    answer = ask("Press Enter to re-check it, type or drag in the path of a new CSV, or 'q' to quit: ")
    if answer.lower() in ("q", "quit", "exit"):
        print("Stopped. No MoltSets lookups were run.")
        sys.exit(0)
    # terminals quote or backslash-escape dragged-in paths
    return os.path.expanduser(answer.strip("'\"").replace("\\ ", " ")) if answer else path


def precheck(load, path, noun):
    """Validate inputs + budget before anything billed runs; loop until clean or confirmed.

    load(path) -> (items, issues, blocking). Returns (items, path) — path may have been
    replaced by the user.
    """
    while True:
        if path:
            print(f"\nChecking {path} ...")
        items, issues, blocking = load(path)
        if not issues:
            print(f"   Pre-run check passed: {plural(len(items), noun)} ready.")
            return items, path
        report_issues(issues)
        if not blocking and ask(f"\nContinue anyway with {plural(len(items), noun)}? [y/N] ").lower() in ("y", "yes"):
            return items, path
        if not path:
            remind_if_search_exhausted()
            sys.exit("Stopped before any MoltSets lookup. Fix the issue(s) above and re-run.")
        path = wait_for_reupload(path)


def read_csv(path):
    """Returns (fieldnames, rows, columns) where columns maps normalized header -> real header."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Cells are someone else's text (a bought lead list, a web log): drop control and invisible characters.
        rows = [{clean_text(k, None): clean_text(v, None, keep_newlines=True) for k, v in row.items()}
                for row in reader]
        fieldnames = [clean_text(h, None) for h in reader.fieldnames or [] if h]
    columns = {re.sub(r"[\s-]+", "_", h.strip().lower()): h for h in fieldnames}
    return fieldnames, rows, columns


def csv_error(path, e):
    if isinstance(e, OSError):
        return f"Can't read {path}: {e.strerror}."
    return f"Can't parse {path} ({e}). In Excel, save it as 'CSV UTF-8'."


def column(columns, candidates):
    return next((columns[c] for c in candidates if c in columns), None)


def cell(row, col):
    """One input cell as a single line of at most MAX_TEXT characters: this is the value that gets printed,
    sent to the API and matched on, so a line break in a bought lead list can't fake a line of console output.
    The row itself is left as read, and is what an output CSV carries over."""
    value = row.get(col) if col else ""
    return clean_text(value).strip() if isinstance(value, str) else ""


def write_csv(path, rows):
    fieldnames = []
    for row in rows:
        fieldnames += [k for k in row if k not in fieldnames]
    write_table(path, fieldnames, rows)  # formula-safe cells: these files are opened in spreadsheets


def grade_summary(rows, key="email_risk_score"):
    grades = {}
    for r in rows:
        if r.get(key):
            grades[r[key]] = grades.get(r[key], 0) + 1
    return ", ".join(f"{g}: {n}" for g, n in sorted(grades.items())) or "none"


def load_exclusions(paths, extra):
    """Domains to suppress, from CSV files (any domain/website/email column) plus a comma list."""
    domains, issues = set(), []
    for path in paths:
        try:
            _, rows, columns = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            issues.append(csv_error(path, e) + " Its companies won't be excluded.")
            continue
        domain_col, email_col = column(columns, DOMAIN_COLUMNS), column(columns, EMAIL_COLUMNS)
        if not (domain_col or email_col):
            issues.append(f"--exclude-file {path}: no website/domain or email column — nothing excluded from it.")
        for row in rows:
            if d := valid_domain(cell(row, domain_col)):
                domains.add(d)
            for e in split_emails(cell(row, email_col)):
                if not is_personal_domain(email_domain(e)):
                    domains.add(email_domain(e))
    domains |= {d for d in (valid_domain(t) for t in split_terms(extra)) if d}
    return sorted(domains), issues
