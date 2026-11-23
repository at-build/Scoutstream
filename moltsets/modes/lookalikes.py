"""lookalikes mode: companies like a set of seed accounts (search_companies), ranked by a similarity score."""

import csv
import os
import shlex

from moltsets.api import (
    STATE,
    SearchLimitReached,
    StopRun,
    ToolError,
    check_budget,
    describe_search_allowance,
    paged_search,
    remind_if_search_exhausted,
)
from moltsets.companies import company_row, lookup_company
from moltsets.config import (
    DOMAIN_COLUMNS, EMPLOYEE_RANGES, MAX_EXCLUDE_DOMAINS, MAX_SEEDS, REVENUE_RANGES, SEARCH_WINDOW,
)
from moltsets.csvio import ask, cell, column, csv_error, load_exclusions, output_path, precheck, read_csv, write_csv
from moltsets.util import domain_stem, match_vocabulary, normalize_country, plural, slug, split_terms, valid_domain


# Points per signal. For NAICS only the longest shared prefix counts.
NAICS_POINTS = ((6, 40), (4, 30), (3, 15), (2, 5))
LINKEDIN_POINTS = 25
EMPLOYEE_POINTS = (15, 8)  # same band, one band away
REVENUE_POINTS = (10, 5)
COUNTRY_POINTS = 10


def build_profile(seeds, args):
    """What the seeds share, with --naics / --linkedin-industry / --employee-range / --country / --any-country
    replacing the matching part. Returns (profile, notes).

    Headcount bands are widened by one band each way; revenue is only used for scoring, because a positive
    revenue filter would drop the many companies with no revenue on record.
    """
    notes = []
    naics6 = sorted({code for s in seeds for code in s.get("naics_codes") or []})
    linkedin = sorted({v for s in seeds for v in s.get("linkedin_industry") or []})
    bands = set()
    for s in seeds:
        if s.get("employee_range") in EMPLOYEE_RANGES:
            i = EMPLOYEE_RANGES.index(s["employee_range"])
            bands.update(EMPLOYEE_RANGES[max(0, i - 1):i + 2])
    employee = [r for r in EMPLOYEE_RANGES if r in bands]
    countries = sorted({s["country"] for s in seeds if s.get("country")})
    if args.naics:
        naics6 = split_terms(args.naics)
    if args.linkedin_industry:
        linkedin = split_terms(args.linkedin_industry, ";")
    if args.employee_range:
        matched, unmatched = match_vocabulary(split_terms(args.employee_range), EMPLOYEE_RANGES)
        if unmatched:
            notes.append(f"--employee-range: {unmatched} aren't MoltSets values and are dropped. "
                         f"Valid: {EMPLOYEE_RANGES}.")
        if matched:
            employee = matched
    if args.any_country:
        countries = []
    elif args.country:
        countries = [normalize_country(c) for c in split_terms(args.country)]
    return {
        "naics6": naics6,
        "naics4": sorted({code[:4] for code in naics6 if len(code) >= 4}),
        "linkedin_industry": linkedin,
        "employee_range": employee,
        "country": countries,
        "industry": sorted({s["industry"] for s in seeds if s.get("industry")}),
        "revenue_range": [r for r in REVENUE_RANGES if r in {s.get("revenue_range") for s in seeds}],
    }, notes


def tiers(profile):
    """search_companies filters per tier, closest first: exact NAICS + LinkedIn industry, then 4-digit NAICS.
    Without NAICS codes: one tier on LinkedIn industry, else on the broad industry bucket."""
    common = {k: profile[k] for k in ("employee_range", "country") if profile[k]}
    if profile["naics6"]:
        first = {**common, "naics_code": profile["naics6"]}
        if profile["linkedin_industry"]:
            first["linkedin_industry"] = profile["linkedin_industry"]
        second = {**common, "naics_code": profile["naics4"]}
        return [first] + ([second] if profile["naics4"] and second != first else [])
    if profile["linkedin_industry"]:
        return [{**common, "linkedin_industry": profile["linkedin_industry"]}]
    if profile["industry"]:
        return [{**common, "industry": profile["industry"]}]
    return []


def _band_distance(a, b, ladder):
    return abs(ladder.index(a) - ladder.index(b)) if a in ladder and b in ladder else None


def similarity(candidate, seed):
    """(points 0-100, reasons) for one candidate against one seed."""
    points, reasons = 0, []
    codes, seed_codes = candidate.get("naics_codes") or [], seed.get("naics_codes") or []
    for length, value in NAICS_POINTS:
        shared = sorted({c[:length] for c in codes if len(c) >= length}
                        & {s[:length] for s in seed_codes if len(s) >= length})
        if shared:
            points += value
            title = ""
            if shared[0] in codes:
                titles = candidate.get("naics_titles") or []
                i = codes.index(shared[0])
                title = f" {titles[i]}" if i < len(titles) and titles[i] else ""
            reasons.append(f"NAICS {shared[0]}{title}")
            break
    shared_linkedin = sorted(set(candidate.get("linkedin_industry") or []) & set(seed.get("linkedin_industry") or []))
    if shared_linkedin:
        points += LINKEDIN_POINTS
        reasons.append(f"LinkedIn: {shared_linkedin[0]}")
    for key, ladder, (same, near), label in (
        ("employee_range", EMPLOYEE_RANGES, EMPLOYEE_POINTS, "{} employees"),
        ("revenue_range", REVENUE_RANGES, REVENUE_POINTS, "revenue {}"),
    ):
        distance = _band_distance(candidate.get(key), seed.get(key), ladder)
        if distance == 0:
            points += same
            reasons.append(label.format(candidate[key]))
        elif distance == 1:
            points += near
            reasons.append(f"{label.format(candidate[key])} (seed: {seed[key]})")
    if candidate.get("country") and candidate.get("country") == seed.get("country"):
        points += COUNTRY_POINTS
        reasons.append(candidate["country"])
    return points, reasons


def best_match(candidate, seeds):
    """(points, seed domain, reasons) for the seed the candidate is most like; the first seed wins a tie."""
    best = (-1, None, [])
    for seed in seeds:
        points, reasons = similarity(candidate, seed)
        if points > best[0]:
            best = (points, seed.get("domain"), reasons)
    return best


def load_seeds(args, path):
    """Seed domains from the --seeds CSV and --seed, deduplicated. Returns (domains, issues, blocking)."""
    domains, issues = [], []
    if path:
        try:
            _, rows, columns = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            return [], [csv_error(path, e)], True
        domain_col = column(columns, DOMAIN_COLUMNS)
        if not domain_col:
            return [], [f"{path} needs a website/domain column (`website`, `domain` or `company_domain`); "
                        f"found {list(columns.values())}."], True
        for line_no, row in enumerate(rows, start=2):
            raw = cell(row, domain_col)
            domain = valid_domain(raw)
            if domain and domain not in domains:
                domains.append(domain)
            elif raw and not domain:
                issues.append(f"line {line_no}: {raw!r} isn't a valid domain — skipped.")
    for term in split_terms(args.seed):
        domain = valid_domain(term)
        if not domain:
            issues.append(f"--seed {term!r} isn't a valid domain — skipped.")
        elif domain not in domains:
            domains.append(domain)
    if not domains:
        return [], issues + ["No seed domains: pass --seeds FILE and/or --seed domain,domain."], True
    if len(domains) > MAX_SEEDS:
        return domains, issues + [f"{len(domains)} seeds; the most is {MAX_SEEDS}. Keep your best-fit accounts."], True
    return domains, issues, False


def split_exclusions(excluded, priority):
    """MoltSets takes at most MAX_EXCLUDE_DOMAINS per search. Send the ones most likely to come back (priority:
    companies already returned, then the seeds), and return the rest to filter locally."""
    ordered = sorted(excluded, key=lambda d: (d not in priority, d))
    return ordered[:MAX_EXCLUDE_DOMAINS], set(ordered[MAX_EXCLUDE_DOMAINS:])


def lookalikes_path(args):
    if args.seeds:
        return f"{slug(os.path.splitext(os.path.basename(args.seeds))[0])}_lookalikes.csv"
    first = split_terms(args.seed)[0]
    return f"lookalikes_{slug(domain_stem(valid_domain(first)) or first)}.csv"


def run_lookalikes(args):
    def load(path):
        domains, issues, blocking = load_seeds(args, path)
        if blocking:
            return domains, issues, True
        budget_issues, budget_blocking = check_budget(search_needed=len(domains) + args.max_results,
                                                      search_required=True)
        return domains, issues + budget_issues, budget_blocking

    domains, args.seeds = precheck(load, args.seeds, "seed")
    print(f"\nLooking up {plural(len(domains), 'seed')} (1 search result each if MoltSets has it; cached) ...")
    seeds, missing = [], []
    try:
        for domain in domains:
            found = lookup_company(domain)
            (seeds if found else missing).append(found or domain)
    except (SearchLimitReached, StopRun, ToolError) as e:
        print(f"   Stopped looking up seeds: {e}")
    if missing:
        print(f"   Not in MoltSets (no cost): {', '.join(missing)}")
    if not seeds:
        print("\nNo seed is in MoltSets — nothing to model lookalikes on. No CSV written.")
        return

    profile, notes = build_profile(seeds, args)
    for note in notes:
        print(f"   Note: {note}")
    searches = tiers(profile)
    if not searches:
        print("\nThe seeds have no NAICS code, LinkedIn industry or industry on record. "
              "Pass --naics or --linkedin-industry.")
        return
    print(f"\nProfile from {plural(len(seeds), 'seed')}:")
    for key in ("naics6", "naics4", "linkedin_industry", "employee_range", "country", "revenue_range"):
        if profile[key]:
            print(f"   {key}: {profile[key]}")
    if "industry" in searches[0]:
        print("   (No NAICS code or LinkedIn industry on the seeds: searching their broad industry bucket, "
              "so expect loose matches.)")
    excluded, exclusion_issues = load_exclusions(args.exclude_file, args.exclude_domain)
    for issue in exclusion_issues:
        print(f"   Note: {issue}")
    seed_domains = set(domains) | {s["domain"] for s in seeds if s.get("domain")}
    excluded = set(excluded) | seed_domains
    print(f"   Excluding {plural(len(excluded), 'company')} (seeds + your lists): never returned, so free")
    if len(excluded) > MAX_EXCLUDE_DOMAINS:
        print(f"   MoltSets takes at most {MAX_EXCLUDE_DOMAINS} exclusions per search: the other "
              f"{len(excluded) - MAX_EXCLUDE_DOMAINS} are filtered here, and each one returned costs a search result.")

    found = []  # (company, tier)
    returned_excluded = set()  # excluded companies MoltSets returned anyway (they were over the cap)

    def fetch(filters, need):
        """One search of up to `need` companies, with the exclusions MoltSets won't take applied locally.
        Returns (kept, total, newly dropped domains)."""
        have = {c.get("domain") for c, _ in found if c.get("domain")} | returned_excluded
        sent, local = split_exclusions(excluded | have, have | seed_domains)
        batch, total = paged_search("search_companies", {**filters, "exclude_domain": sent}, need)
        kept = [c for c in batch if c.get("domain") not in local]
        dropped = {c.get("domain") for c in batch if c.get("domain") in local}
        new = dropped - returned_excluded
        returned_excluded.update(dropped)
        return kept, total, new

    try:
        probe, total, _ = fetch(searches[0], 1)
        found += [(c, 1) for c in probe]
        print(f"\nTest search: tier 1 matches {total}{'+' if total >= SEARCH_WINDOW else ''} companies "
              f"({plural(len(probe) + len(returned_excluded), 'search result')} spent).")
        if not total and len(searches) == 1:
            print("Nothing matches. Widen with a shorter --naics code, --employee-range or --any-country.")
            return
        if not args.yes and ask(f"Fetch up to {args.max_results} lookalikes (up to {args.max_results - len(found)} "
                                "more search results)? [y/N] ").lower() not in ("y", "yes"):
            print("Stopped after the test search. No CSV written.")
            return
        for tier, filters in enumerate(searches, start=1):
            if args.max_results - len(found) <= 0 or STATE["search_blocked"]:
                break
            got = 0
            while True:
                need = args.max_results - len(found)
                if need <= 0 or STATE["search_blocked"]:
                    break
                kept, total, new = fetch(filters, need)
                found += [(c, tier) for c in kept]
                got += len(kept)
                # Fetch again only when a locally excluded company took a slot and will now be excluded by MoltSets;
                # a tier that returned nothing new is exhausted.
                if not new or len(kept) + len(new) < need:
                    break
            print(f"   Tier {tier}: {plural(got, 'company')} returned (total matching: {total})")
    except (SearchLimitReached, StopRun, ToolError, KeyboardInterrupt) as e:
        print(f"   Stopped: {str(e) or 'interrupted'}. Keeping the {plural(len(found), 'company')} found so far.")
    if returned_excluded:
        print(f"   {plural(len(returned_excluded), 'company')} on your exclusion lists came back anyway "
              f"(over the {MAX_EXCLUDE_DOMAINS}-domain cap) and were filtered here: "
              f"{plural(len(returned_excluded), 'search result')} spent on them.")

    rows = []
    for c, tier in found:
        points, seed, reasons = best_match(c, seeds)
        rows.append({**company_row(c, c.get("country") or ""), "similarity_score": points,
                     "most_similar_seed": seed, "match_reasons": "; ".join(reasons), "tier": tier})
    rows.sort(key=lambda r: (-r["similarity_score"], r["tier"], -(r["moltsets_score"] or 0)))
    for r in rows:
        print(f"  {r['similarity_score']:>3}  {r['company']} ({r['domain']}) — like {r['most_similar_seed']}: "
              f"{r['match_reasons'] or 'no shared signals'}")
    print(f"\n  Search results left: {describe_search_allowance()}")
    remind_if_search_exhausted()
    if not rows:
        print("\nNo lookalikes found; no CSV written.")
        return
    output = output_path(args.output or lookalikes_path(args))
    write_csv(output, rows)
    print(f"\nSaved {plural(len(rows), 'company')} to {output}")
    print(f"Next: python3 moltsets_poc.py prospect --companies-file {shlex.quote(output)} --ignore-location-column")
