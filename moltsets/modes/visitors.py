"""visitors mode: a website-visitor log -> visiting companies (ip_to_company) -> contacts there (prospect_company)."""

import csv
import ipaddress
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from moltsets.api import (
    STATE,
    SearchLimitReached,
    StopRun,
    ToolError,
    call,
    check_budget,
    describe_search_allowance,
    fetch_account,
    is_cached,
    remind_if_search_exhausted,
)
from moltsets.companies import lookup_company
from moltsets.config import (
    BOT_RE,
    CACHE_TTL_SECONDS,
    IP_COLUMNS,
    PAGE_COLUMNS,
    STATIC_ASSET_RE,
    TIME_COLUMNS,
    USER_AGENT_COLUMNS,
    VISITOR_BLOCKLIST,
    VISITORS_PATH,
    WEAK_IP_MATCH,
)
from moltsets.csvio import (
    cell,
    column,
    csv_error,
    grade_summary,
    load_exclusions,
    output_path,
    read_csv,
    report_issues,
    write_csv,
)
from moltsets.safety import clean_text, open_private
from moltsets.people import SEARCH_STATUS_COLUMN, contact_columns, prospect_company, resolve_seniority, resolve_titles
from moltsets.util import (
    domain_stem,
    is_academic_domain,
    is_personal_domain,
    normalize_country,
    plural,
    split_terms,
    valid_domain,
)

NGINX_TIME = "%d/%b/%Y:%H:%M:%S %z"
TOP_PAGES = 5


def visitor_ip(raw):
    """(ip, None) for a public IPv4 address, else (None, reason). An X-Forwarded-For list gives its first address."""
    value = (raw or "").split(",")[0].strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None, "skipped: not an IP address"
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.version == 6:
        return None, "skipped: IPv6 (MoltSets resolves IPv4 only)"
    if not address.is_global:
        return None, "skipped: private or reserved address"
    return str(address), None


def parse_time(value):
    """A log timestamp as a UTC datetime, or None. Accepts ISO 8601, epoch seconds / ms / µs / ns (Cloudflare's
    EdgeStartTimestamp) and the nginx / Apache format; a time without a zone is taken as UTC."""
    value = (value or "").strip().strip("[]")
    if not value or len(value) > 40:  # no real timestamp is longer; a huge number would never finish dividing
        return None
    if re.fullmatch(r"\d+(\.\d+)?", value):
        n = float(value)
        while n > 1e11:  # milli-, micro- or nanoseconds
            n /= 1000
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    for parse in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
                  lambda s: datetime.strptime(s, NGINX_TIME)):
        try:
            when = parse(value)
        except ValueError:
            continue
        return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
    return None


MAX_PAGE_PATH = 200


def page_path(value):
    """The path of a page URL or path ("https://example.com/pricing?x=1" -> "/pricing"); "" when there's none.

    Any visitor can request any URL, so a path is whatever a stranger typed: it's cleaned and cut to
    MAX_PAGE_PATH characters before it reaches the console, the record file or the CSV."""
    return clean_text(_page_path(value), MAX_PAGE_PATH)


def _page_path(value):
    value = (value or "").strip()
    if not value:
        return ""
    if re.match(r"^https?://", value):
        try:
            return urlparse(value).path or "/"
        except ValueError:  # junk such as "https://[bad" (scanners): take what follows the host
            after_host = value.split("://", 1)[1]
            value = after_host[after_host.find("/"):] if "/" in after_host else ""
    return value.split("?")[0].split("#")[0] or "/"


def parse_log(path):
    """Read a visitor log CSV. Returns (visits, dropped, issues, blocking).

    visits: {ip: {"views": int, "pages": Counter, "first": datetime or None, "last": datetime or None}}.
    dropped: {reason: rows}. Only an unreadable file or a missing IP column is blocking: a log with no usable
    rows comes back with no visits and no issue, so a scheduled run ends quietly.
    """
    try:
        _, rows, columns = read_csv(path)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        return {}, {}, [csv_error(path, e)], True
    ip_col = column(columns, IP_COLUMNS)
    if not ip_col:
        headers = list(columns.values())
        hint = " It looks semicolon-separated: export it as a comma-separated CSV." \
            if any(";" in h for h in headers) else ""
        return {}, {}, [f"{path} needs an IP column ({', '.join(IP_COLUMNS)}); found {headers}.{hint}"], True
    time_col, page_col, ua_col = (column(columns, c) for c in (TIME_COLUMNS, PAGE_COLUMNS, USER_AGENT_COLUMNS))
    visits, dropped = {}, Counter()
    for row in rows:
        if ua_col and BOT_RE.search(cell(row, ua_col)):
            dropped["skipped: bot user agent"] += 1
            continue
        ip, reason = visitor_ip(cell(row, ip_col))
        if not ip:
            dropped[reason] += 1
            continue
        visit = visits.setdefault(ip, {"views": 0, "pages": Counter(), "first": None, "last": None})
        page = page_path(cell(row, page_col))
        if not STATIC_ASSET_RE.search(page):
            visit["views"] += 1
            if page:
                visit["pages"][page] += 1
        when = parse_time(cell(row, time_col))
        if when:
            visit["first"] = min(visit["first"] or when, when)
            visit["last"] = max(visit["last"] or when, when)
        elif cell(row, time_col):
            dropped["kept: timestamp not understood"] += 1
    return visits, dict(dropped), [], False


def parse_pct(value):
    """ip_to_company's percentage ("87", "87.5%", 87) as a float; None when missing or unreadable."""
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def resolve_ips(ips):
    """ip_to_company for each IP, in order: 1 token per match, misses free, cached 30 days.

    Returns ({ip: (domain, pct)}, stopped). Of several candidate companies the highest percentage wins. If
    MoltSets stops the run (e.g. the enrichment allowance) or Ctrl+C is pressed, what was resolved so far comes
    back with the reason.
    """
    resolved, failed = {}, 0
    for ip in ips:
        try:
            status, res = call("ip_to_company", {"ip_address": ip})
        except ToolError:
            failed += 1
            continue
        except (StopRun, KeyboardInterrupt) as e:
            return resolved, "interrupted" if isinstance(e, KeyboardInterrupt) else str(e).rstrip(".")
        best = None
        for candidate in (res.get("companies") or []) if status == "ok" else []:
            domain, pct = valid_domain(candidate.get("domain")), parse_pct(candidate.get("percentage"))
            if domain and (best is None or (pct or 0) > (best[1] or 0)):
                best = (domain, pct)
        if best:
            resolved[ip] = best
    if failed:
        print(f"   {plural(failed, 'IP lookup')} failed (MoltSets error) and were skipped.")
    return resolved, None


def group_companies(visits, resolved):
    """Add up the visits per company domain. Returns {domain: company}."""
    companies = {}
    for ip, (domain, pct) in resolved.items():
        visit = visits[ip]
        c = companies.setdefault(domain, {"domain": domain, "ips": 0, "views": 0, "pages": Counter(),
                                          "first": None, "last": None, "ip_match_pct": None})
        c["ips"] += 1
        c["views"] += visit["views"]
        c["pages"].update(visit["pages"])
        if visit["first"]:
            c["first"] = min(c["first"] or visit["first"], visit["first"])
        if visit["last"]:
            c["last"] = max(c["last"] or visit["last"], visit["last"])
        if pct is not None:
            c["ip_match_pct"] = pct if c["ip_match_pct"] is None else max(c["ip_match_pct"], pct)
    return companies


def block_reason(domain, own_domains, excluded):
    """Why a visiting company isn't worth searching, or None. Matched on the organisation part of the domain, so
    acme.test counts as acme.example."""
    stem = domain_stem(domain)
    if stem in {domain_stem(d) for d in own_domains}:
        return "your own domain"
    if stem in {domain_stem(d) for d in excluded}:
        return "on your exclude list"
    if stem in VISITOR_BLOCKLIST:
        return "internet provider, cloud or proxy"
    if is_personal_domain(domain):
        return "personal email provider"
    if is_academic_domain(domain):
        return "university"
    return None


def search_group(entry, today):
    """Where a company stands, from its record: 0 held over from an earlier run, 1 not searched (or not in the
    last 30 days), 2 searched in the last 30 days, so its lookups come from the cache (unless --refresh)."""
    if entry.get("pending"):
        return 0
    try:
        age = (today - date.fromisoformat(entry["last_searched_run"])).days
    except (KeyError, TypeError, ValueError):
        return 1
    return 2 if not STATE["refresh"] and age < CACHE_TTL_SECONDS // 86400 else 1


def order_companies(companies, ledger, today=None):
    """Companies held over from an earlier run first, then those not searched yet, then those searched in the
    last 30 days; within each, most page views, then most recent visit."""
    today = today or date.today()

    def key(c):
        group = search_group(ledger["companies"].get(c["domain"], {}), today)
        return group, -c["views"], -(c["last"].timestamp() if c["last"] else 0)
    return sorted(companies, key=key)


def stored_company(domain, entry):
    """A held-over company rebuilt from its record, for a run whose log no longer has it."""
    stats = entry.get("stats") or {}
    return {"domain": domain, "ips": stats.get("ips", 0), "views": stats.get("views", 0),
            "pages": Counter(stats.get("pages") or {}), "first": parse_time(stats.get("first_seen")),
            "last": parse_time(stats.get("last_seen") or entry.get("last_visit")),
            "ip_match_pct": stats.get("ip_match_pct")}


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else ""


def load_ledger(path):
    try:
        with open(path, encoding="utf-8") as f:
            ledger = json.load(f)
        if isinstance(ledger.get("companies"), dict):
            return ledger
    except (OSError, ValueError, AttributeError):
        pass
    return {"companies": {}}


def save_ledger(ledger, path):
    tmp = f"{path}.tmp"
    with open_private(tmp) as f:
        json.dump(ledger, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def update_ledger(ledger, companies, today):
    """Record this run: first and last run seen, latest visit, whether the company is held over, when it was
    searched (its company lookup ran and its search finished), and its visit stats (no IPs, only their count)."""
    for c in companies:
        entry = ledger["companies"].setdefault(c["domain"], {"first_seen_run": today})
        entry["last_run"] = today
        if c["last"]:
            entry["last_visit"] = max(entry.get("last_visit") or "", iso(c["last"]))
        entry["pending"] = c["pending"]
        if c.get("searched") and not c["pending"]:
            entry.setdefault("first_searched_run", today)
            entry["last_searched_run"] = today
        entry["stats"] = {"views": c["views"], "ips": c["ips"], "pages": dict(c["pages"].most_common(TOP_PAGES)),
                          "first_seen": iso(c["first"]), "last_seen": iso(c["last"]),
                          "ip_match_pct": c["ip_match_pct"]}


def search_fit(n, per_company):
    """How many of n companies the search results left can cover at worst-case cost (a free account check)."""
    try:
        fetch_account()
    except (StopRun, ToolError, SearchLimitReached):
        pass  # keep the figure from the pre-run check
    left = STATE["search_remaining"]
    return n if left is None else max(0, min(n, left // per_company))


def search_visitors(companies, titles, wanted_levels, args):
    """Firmographics, then contacts, for each company in order. Sets name / firmographics / leads / status /
    pending on each. Returns why the run stopped early, or None."""
    stopped = None
    for i, c in enumerate(companies, start=1):
        if stopped or STATE["search_blocked"]:
            c.update(status="held over: " + ("run stopped" if stopped else "search allowance used up"), pending=True)
            continue
        print(f"\n##### [{i}/{len(companies)}] {c['domain']} — {plural(c['views'], 'page view')} #####")
        target = None
        try:
            firm = lookup_company(c["domain"])
            c["searched"] = True
            if not firm:
                c["status"] = "not in MoltSets"
                print("   Not in MoltSets (no cost).")
                continue
            c.update(name=firm.get("name") or c["domain"], industry=firm.get("industry"),
                     employee_range=firm.get("employee_range"), country=firm.get("country"))
            target = {"name": c["name"], "domain": c["domain"], "line": None,
                      "countries": [normalize_country(x) for x in args.location],
                      "hq_countries": [c["country"]] if c["country"] else []}
            prospect_company(target, titles, wanted_levels, args)
            c["leads"], c["status"] = target["leads"], target["status"]
            if c["status"].startswith("not searched"):
                c.update(status="held over: search allowance used up", pending=True)
        except SearchLimitReached as e:  # from the firmographic lookup
            STATE["search_blocked"] = str(e)
            c.update(status="held over: search allowance used up", pending=True)
        except ToolError as e:
            c["status"] = f"lookup failed: {e}"
        except (StopRun, KeyboardInterrupt) as e:
            stopped = "interrupted" if isinstance(e, KeyboardInterrupt) else str(e).rstrip(".")
            print(f"   Stopping: {stopped}. Saving results so far.")
            c["leads"] = target.get("leads", []) if target else []
            c.update(status=f"{plural(len(c['leads']), 'contact')} found before the run stopped ({stopped})",
                     pending=True)
    return stopped


def visitor_rows(companies):
    """One row per contact, visit columns first; one row for a company with none. Never any IP address."""
    rows = []
    for c in companies:
        pct = c["ip_match_pct"]
        base = {
            "visitor_company": c["name"],
            "visitor_domain": c["domain"],
            "visitor_industry": c["industry"],
            "visitor_employee_range": c["employee_range"],
            "visitor_country": c["country"],
            "ip_match_pct": "" if pct is None else f"{pct:g}",
            "visitor_ip_count": c["ips"],
            "page_views": c["views"],
            "top_pages": "; ".join(path for path, _ in c["pages"].most_common(TOP_PAGES)),
            "first_seen": iso(c["first"]),
            "last_seen": iso(c["last"]),
            "new_company": "yes" if c["new"] else "no",
            "visitor_notes": f"weak IP match ({pct:g}%)" if pct is not None and pct < WEAK_IP_MATCH else "",
            SEARCH_STATUS_COLUMN: c["status"],
        }
        rows += [{**base, **contact_columns(lead)} for lead in c["leads"]] or [base]
    return rows


def ips_to_resolve(ips, uncached, args):
    """The IPs to resolve this run, busiest first: every cached one (free), and as many uncached ones as the
    enrichment records left can cover while keeping room for the contacts' email lookups. Unknown allowance: all."""
    if not uncached:
        return ips
    try:
        enrich_left = fetch_account()["enrich_left"]  # free
    except (StopRun, ToolError, SearchLimitReached):
        enrich_left = None
    if enrich_left is None:
        return ips
    per_company = 0 if args.no_email_fallback else args.max_results + args.founders
    reserve = min(args.max_companies, len(ips)) * per_company
    allowed = set(uncached[:max(0, enrich_left - reserve)])
    if len(allowed) < len(uncached):
        print(f"   {plural(len(uncached) - len(allowed), 'uncached IP')} wait for the next run "
              f"({enrich_left} enrichment records left this window, {reserve} kept for contacts' emails).")
    uncached = set(uncached)
    return [ip for ip in ips if ip in allowed or ip not in uncached]


def search_in_order(companies, titles, wanted_levels, args):
    """Search the companies, already in order. Those searched in the last 30 days go first: they come from the
    cache, so they're outside --max-companies and the budget, and every run's CSV has them. The others are
    searched up to --max-companies and what the search results left cover at worst-case cost; the rest are held
    over."""
    cached = [c for c in companies if c["cached"]]
    others = [c for c in companies if not c["cached"]]
    stopped = None
    if cached:
        print(f"\nRe-reading {plural(len(cached), 'company')} searched in the last 30 days (from the cache, "
              "no cost) ...")
        stopped = search_visitors(cached, titles, wanted_levels, args)
    if stopped:
        for c in others:
            c.update(status=f"held over: run stopped ({stopped})", pending=True)
        return
    per_company = 1 + args.max_results + args.founders
    cap = min(len(others), args.max_companies)
    fit = search_fit(cap, per_company) if cap else 0
    for c in others[fit:cap]:
        c.update(status="held over: not enough search results left this window", pending=True)
    for c in others[cap:]:
        c.update(status="held over: over --max-companies", pending=True)
    if fit < len(others):
        print(f"\nSearching {fit} of {plural(len(others), 'company')} now (up to {per_company} search results "
              f"each); the other {len(others) - fit} are held over for the next run.")
    search_visitors(others[:fit], titles, wanted_levels, args)


def finish(companies, ledger, today, args, summary):
    """Record the run, print the summary and write the CSV. Runs whatever ended the search (Ctrl+C, an error)."""
    for c in companies:
        if not c["status"]:  # never reached
            c.update(status="held over: run stopped", pending=True)
    if companies:
        update_ledger(ledger, companies, today)
        save_ledger(ledger, VISITORS_PATH)
    leads = [lead for c in companies for lead in c["leads"]]
    held = sum(1 for c in companies if c["pending"])
    print("\n=== Summary ===")
    print(f"  Visitor IPs: {summary['ips']}, matched to a company: {summary['matched']}")
    print(f"  Companies: {summary['found']} found, {summary['ignored']} ignored, "
          + (f"{summary['carried']} added back from earlier runs, " if summary["carried"] else "")
          + f"{len(companies) - held} looked up, {held} held over for the next run")
    print(f"  Leads: {len(leads)}, {sum(1 for r in leads if r['email'])} with email (grades {grade_summary(leads)})")
    print(f"  Search results left: {describe_search_allowance()}")
    remind_if_search_exhausted()
    if not companies:
        print("\nNo visiting companies left after filtering. No CSV written.")
        return
    output = output_path(args.output or f"visitor_leads_{today}.csv")
    rows = visitor_rows(companies)
    write_csv(output, rows)
    print(f"\nSaved {output}: {plural(len(rows), 'row')} for {plural(len(companies), 'visiting company')} "
          "(one per contact, one per company with none)")


def run_visitors(args):
    titles, _ = resolve_titles(args.departments)
    wanted_levels, _, unmatched = resolve_seniority(args.seniority)
    STATE["phones"] = False  # no phone lookups here

    print(f"\nReading {args.log} ...")
    visits, dropped, issues, blocking = parse_log(args.log)
    if blocking:
        report_issues(issues)
        sys.exit("Stopped before any MoltSets lookup. Fix the log export and re-run.")
    for reason, n in sorted(dropped.items()):
        print(f"   {plural(n, 'row')} — {reason}")
    excluded, exclusion_issues = load_exclusions(args.exclude_file, args.exclude_domain)
    if exclusion_issues:  # never prospect customers or competitors unattended because a file moved
        report_issues(exclusion_issues)
        sys.exit("Stopped before any MoltSets lookup. Fix the exclude file(s) and re-run.")
    ledger = load_ledger(VISITORS_PATH)
    if not visits and not any(entry.get("pending") for entry in ledger["companies"].values()):
        print("Nothing to look up: no public IPv4 visits in the log. No CSV written.")
        return
    if unmatched:
        print(f"   Note: --seniority {unmatched} matched no MoltSets level; using {wanted_levels or 'all levels'}.")
    own = {d for d in (valid_domain(t) for t in split_terms(args.own_domain)) if d}
    if not own:
        print("   Note: no --own-domain given — visits from your own company will be prospected too.")
    ips = sorted(visits, key=lambda ip: -visits[ip]["views"])  # busiest first, so a stop keeps the best
    uncached = [ip for ip in ips if not is_cached("ip_to_company", {"ip_address": ip})]
    print(f"   {plural(len(ips), 'visitor IP')} to resolve ({len(ips) - len(uncached)} from the cache)" if ips
          else "   No public IPv4 visits in the log; searching the companies held over from earlier runs.")
    budget_issues, budget_blocking = check_budget(tokens_needed=len(uncached))
    if budget_blocking:
        report_issues(budget_issues)
        sys.exit("Stopped before any MoltSets lookup.")
    for issue in budget_issues:
        print(f"   Note: {issue}")

    resolved, stopped = {}, None
    if ips:
        now = ips_to_resolve(ips, uncached, args)
        print(f"\nResolving {plural(len(now), 'IP')} to companies (1 token per match, misses free) ...")
        resolved, stopped = resolve_ips(now)
        if stopped:
            print(f"   Stopped resolving: {stopped}. The rest are resolved next run.")
    grouped = group_companies(visits, resolved)
    if ips:
        print(f"   {len(resolved)} of {plural(len(ips), 'IP')} matched {plural(len(grouped), 'company')}")

    blocked, kept = Counter(), []
    for c in grouped.values():
        reason = block_reason(c["domain"], own, excluded)
        if reason:
            blocked[reason] += 1
        else:
            kept.append(c)
    for reason, n in sorted(blocked.items()):
        print(f"   Ignored {plural(n, 'company')}: {reason}")
    carried = [stored_company(domain, entry) for domain, entry in ledger["companies"].items()
               if entry.get("pending") and domain not in grouped and not block_reason(domain, own, excluded)]
    if carried:
        print(f"   Added back {plural(len(carried), 'company')} held over from earlier runs but not in this log "
              f"(visit figures from {os.path.basename(VISITORS_PATH)})")
    kept += carried

    today = date.today()
    for c in kept:
        entry = ledger["companies"].get(c["domain"], {})
        c.update(new=not entry.get("first_searched_run"), cached=search_group(entry, today) == 2,
                 name=c["domain"], industry=None, employee_range=None, country=None, leads=[], status="",
                 pending=False)
    kept = order_companies(kept, ledger, today)
    summary = {"ips": len(ips), "matched": len(resolved), "found": len(grouped), "ignored": sum(blocked.values()),
               "carried": len(carried)}
    try:
        if stopped == "interrupted":  # Ctrl+C while resolving: search nothing now
            for c in kept:
                c.update(status="held over: run stopped (interrupted)", pending=True)
        else:  # a MoltSets stop while resolving doesn't touch the search results: search what was resolved
            search_in_order(kept, titles, wanted_levels, args)
    finally:
        finish(kept, ledger, today.isoformat(), args, summary)
