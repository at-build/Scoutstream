"""
POC: GTM prospecting with the MoltSets API (https://developer.moltsets.com/).
One command, seven modes; the code is in the moltsets/ package, one module per mode.
They chain: accounts / lookalikes -> prospect -> (enrich / verify). visitors runs on its own, or on a schedule.

  account    Free. Plan, token balance, search allowance left, phone tokens.

  accounts   Build a target-company list from firmographic filters
             (POST /search_companies). Writes a companies CSV in the same
             company,website,location format the prospect mode (and lusha_poc.py) reads.

  prospect   Find people in given roles at given companies, like lusha_poc.py
             (POST /search_people with company_domain + title keywords + seniority +
             country). Results already carry the business email and its A-F risk grade;
             people returned without one get a /linkedin_to_best_email fallback.

  enrich     Find verified email + contact info for people you already have: by
             LinkedIn URL, by name + company domain, or by email alone. One person from
             the command line, or a CSV.

  verify     Re-check lead files you already have (e.g. leads/*.csv from Lusha,
             Crustdata, Explorium, ...): grade their emails A-F, find emails they're missing,
             flag people who changed company or title, and compare providers side by side.

  visitors   Turn a website-visitor log export into leads: each IP is resolved to a company
             (POST /ip_to_company); internet providers, clouds and your own domain are dropped;
             the procurement / supply-chain leaders and founders there are found as in prospect.
             Never asks and never overruns the search allowance, so it can run from cron.

  lookalikes Find companies like your best-fit seed accounts (POST /search_companies on the
             NAICS codes, LinkedIn industries, size and country they share), ranked by a 0-100
             similarity score. Writes an accounts-format CSV for prospect.

Auth: API key from MOLTSETS_API_KEY in a local .env (or the environment), sent as a
Bearer token. MoltSets also rejects requests without a User-Agent header (403).

Usage:
  echo 'MOLTSETS_API_KEY=ms_your_key_here' >> .env       # one-time setup

  python3 moltsets_poc.py account

  python3 moltsets_poc.py accounts --industry Manufacturing --employee-range 51-200,201-500 \\
      --country India --exclude-file targets.csv --max-results 25

  python3 moltsets_poc.py prospect --company Example Space --website examplespace.example
  python3 moltsets_poc.py prospect --companies-file targets.csv \\
      --departments purchasing,"supply chain" --seniority director+ --location India

  python3 moltsets_poc.py enrich --name "Jane Doe" --company acme.example
  python3 moltsets_poc.py enrich --linkedin https://www.linkedin.com/in/example_some_person
  python3 moltsets_poc.py enrich --prospects-file prospects.csv [--phones]

  python3 moltsets_poc.py verify leads/lusha_examplemotors_motors_leads.csv leads/crustdata_exampleelectric_leads.csv

  python3 moltsets_poc.py visitors --log exports/visits.csv --own-domain example.com
  python3 moltsets_poc.py lookalikes --seed examplemotors.example,examplecycles.example --exclude-file targets.csv

Run `python3 moltsets_poc.py <mode> --help` for each mode's options.

Budget. Two separate allowances matter:
  - search results: every person/company a search returns uses one (accounts, prospect,
    and enrich's name lookups). The free plan has 100 for life, max 5 per call; paid plans
    have rolling 5-hour / weekly pools (e.g. 500 per 5h on $27). Searches that match
    nothing are free.
  - tokens: 1 per lookup that returns data (misses are free). Free plan: 1,000 for life;
    paid plans: unlimited, within fair-use enrichment limits.
  - phone tokens (--phones): a separate paid subscription, 1 per number found.
Every mode checks the account first (free) and warns before a run could exceed what's
left. If an allowance runs out mid-run, the script saves what it has.

Email risk score: MoltSets grades send safety, not whether the address belongs to the
person. A = send, B = probably send, C (catch-all) / F (no signal) = your call,
D = do not send. Every output CSV keeps the grade and an `email_action` column.
"""

import argparse
import os
import sys

from moltsets.config import (
    API_KEY,
    CACHE_PATH,
    DEFAULT_SENIORITY,
    EMPLOYEE_RANGES,
    INDUSTRIES,
    JOB_TITLE_HINTS,
    MAX_SEEDS,
    REVENUE_RANGES,
    SENIORITY_LADDER,
    SENIORITY_TERMS,
)
from moltsets.util import plural
from moltsets.api import STATE
from moltsets.modes.account import run_account
from moltsets.modes.accounts import run_accounts
from moltsets.modes.prospect import run_prospect
from moltsets.modes.enrich import run_enrich
from moltsets.modes.verify import run_verify
from moltsets.modes.lookalikes import run_lookalikes
from moltsets.modes.visitors import run_visitors


def add_people_search_args(p, max_results, founders):
    """The people-search flags prospect and visitors share."""
    p.add_argument("--departments", default="purchasing,supply chain",
                   help='Comma-separated roles (default "purchasing,supply chain"), matched as job-title keywords. '
                        f"Known terms expand: {sorted(JOB_TITLE_HINTS)}; anything else is used as typed "
                        '(e.g. "plant head"); "all" = any title.')
    p.add_argument("--seniority", default=DEFAULT_SENIORITY,
                   help=f'Default "{DEFAULT_SENIORITY}" (Director, Head, VP, Partner, Owner, C Suite). Other terms: '
                        f"{sorted(k for k in SENIORITY_TERMS if k != DEFAULT_SENIORITY)}, a MoltSets level "
                        f"{list(SENIORITY_LADDER)}, or 'all'. People at these levels get the slots first; people "
                        "with no level on record fill the rest.")
    p.add_argument("--location", action="append", default=[],
                   help='Only people based in this country (full English name, e.g. "India"; "USA"/"UK" are '
                        "translated). Repeat for several.")
    p.add_argument("--max-results", type=int, default=max_results,
                   help=f"People per company for the role search (default {max_results}; each one uses a search "
                        "result). Above the per-call max (5 on free, 25 paid) it pages.")
    p.add_argument("--founders", type=int, default=founders, metavar="N",
                   help=f"Also run a separate search for up to N founders / CEOs per company (default {founders}"
                        f"{' = off' if not founders else ''}), so they don't compete with the role search for slots.")
    p.add_argument("--no-email-fallback", action="store_true",
                   help="Don't run a best-email lookup for people the search returns without an email "
                        "(it costs a token per email found, no search results).")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode_names = "account,accounts,prospect,enrich,verify,visitors,lookalikes"
    modes = parser.add_subparsers(dest="mode", required=True, metavar="{" + mode_names + "}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--refresh", action="store_true",
                        help="Ignore cached lookups and ask MoltSets again (paid lookups are cached for 30 days in "
                             f"{os.path.basename(CACHE_PATH)} so re-runs cost nothing).")

    modes.add_parser("account", help="Free: plan, tokens, search allowance, phone tokens.")

    p = modes.add_parser("accounts", help="Build a target-company list (search_companies).", parents=[common],
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--industry", help=f"MoltSets industry bucket(s), comma- or ';'-separated. One of: {INDUSTRIES}")
    p.add_argument("--linkedin-industry",
                   help='LinkedIn industry label(s), ";"-separated, exact (e.g. "Aviation and Aerospace Component Manufacturing")')
    p.add_argument("--naics", help='NAICS code(s), comma-separated, any level (e.g. "3364" aerospace parts, "336" transport equipment)')
    p.add_argument("--employee-range", help=f"Comma-separated: {EMPLOYEE_RANGES}")
    p.add_argument("--revenue-range", help=f"Comma-separated: {REVENUE_RANGES}")
    p.add_argument("--country", help='Company country, comma-separated full names ("India", "United States"). '
                                     "Note: large multinationals are filed under one office's country.")
    p.add_argument("--state", help='State/region, comma-separated ("Karnataka", "TX")')
    p.add_argument("--name", help="Free-text company name search")
    p.add_argument("--domain", help="Exact company domain(s), comma-separated — a firmographic lookup")
    p.add_argument("--exclude-file", action="append", default=[],
                   help="CSV of companies you already have (any website/domain or email column); they're never "
                        "returned, so they cost nothing. Repeatable.")
    p.add_argument("--exclude-domain", help="Comma-separated domains to exclude")
    p.add_argument("--max-results", type=int, default=5,
                   help="Companies to return (default 5). Each one uses a search result.")
    p.add_argument("--location", help="Value for the output's `location` column, i.e. where prospect looks for "
                                      "people at these companies (default: the --country value)")
    p.add_argument("--output", help="Output CSV (default moltsets_accounts.csv)")

    p = modes.add_parser("prospect", help="Find people in given roles at given companies (search_people).",
                         parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--company", action="append", default=[], help="Company name. Repeat for several; pair each with --website.")
    p.add_argument("--website", action="append", default=[], help="Company website/domain, one per --company, same order.")
    p.add_argument("--companies-file",
                   help="CSV with a `website` (or `domain`) column, optional `company` and `location` columns "
                        "(location = country, ';'-separated; overrides --location for that row). "
                        "The accounts mode's output works as-is.")
    add_people_search_args(p, max_results=5, founders=0)
    p.add_argument("--ignore-location-column", action="store_true",
                   help="Don't use the CSV's `location` / `country` column as a filter on where people are based "
                        "(use it when that column is the company's HQ country). --location still applies.")
    p.add_argument("--phones", action="store_true", help="Also look up mobile numbers (phone tokens, paid add-on).")
    p.add_argument("--output", help="Output CSV. With --companies-file: <file>_with_contacts.csv next to it — your "
                                    "rows and columns plus contact_ columns, one row per contact. Otherwise "
                                    "<company>_moltsets_leads.csv. An existing file is never overwritten: a "
                                    "numbered copy is written instead, unless --append.")
    p.add_argument("--append", action="store_true",
                   help="Merge into the output CSV if it exists (same person = same LinkedIn URL, or same name at "
                        "the same company; a newer row replaces an older one unless it has no email)")

    p = modes.add_parser("enrich", help="Emails + contact info for people you already have.", parents=[common],
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", help='One person\'s full name. Needs --company too.')
    p.add_argument("--company", help='Their company website or domain, e.g. "acme.example".')
    p.add_argument("--linkedin", help="Their LinkedIn profile URL (enough on its own; most complete).")
    p.add_argument("--email", help="Their email (enough on its own; e.g. an inbound lead).")
    p.add_argument("--prospects-file",
                   help="CSV with `linkedin_url` and/or `email` and/or `name` (or `first_name` + `last_name`) plus a "
                        "company domain column. Each row needs a LinkedIn URL, an email, or a name + domain.")
    p.add_argument("--phones", action="store_true", help="Also look up mobile numbers (phone tokens, paid add-on).")
    p.add_argument("--output", help="Output CSV (default <file or name>_moltsets_contacts.csv)")

    p = modes.add_parser("verify", help="Grade emails + check job changes in existing lead files.", parents=[common],
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="Lead CSVs (e.g. leads/*.csv). Needs an email and/or LinkedIn column.")
    p.add_argument("--expected-company",
                   help="Employer every lead should be at (name or domain), for files without a company column "
                        "(e.g. crustdata_exampleelectric_leads.csv -> exampleelectric.example)")
    p.add_argument("--limit", type=int, help="Only check the first N rows of each file (to try it cheaply)")
    p.add_argument("--output-dir", help="Where to write <file>_moltsets_verified.csv (default: current folder)")

    p = modes.add_parser("visitors", help="Website-visitor log -> visiting companies -> contacts there "
                                          "(ip_to_company + search_people). Safe to schedule.",
                         parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True,
                   help="CSV export of server / CDN logs with an IP column (ip, client_ip, remote_addr, ClientIP, "
                        "x_forwarded_for, ...); optional time, page / url and user-agent columns.")
    p.add_argument("--own-domain", default="", help="Your website's domain(s), comma-separated; ignored as visitors.")
    add_people_search_args(p, max_results=3, founders=2)
    p.add_argument("--max-companies", type=int, default=50,
                   help="Most companies to search per run (default 50), most-visited first; the rest are held over. "
                        "Companies searched in the last 30 days come from the cache and don't count.")
    p.add_argument("--exclude-file", action="append", default=[],
                   help="CSV of companies to ignore (customers, competitors): any website/domain or email column. "
                        "Repeatable. An unreadable file stops the run before any lookup.")
    p.add_argument("--exclude-domain", help="Comma-separated domains to ignore")
    p.add_argument("--output", help="Output CSV (default visitor_leads_<date>.csv; never overwritten)")
    p.set_defaults(phones=False)

    p = modes.add_parser("lookalikes", help="Companies like your best-fit accounts (search_companies), ranked by "
                                            "similarity.", parents=[common],
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seeds", help="CSV of seed companies: any website / domain column (accounts and prospect "
                                   f"outputs work). Up to {MAX_SEEDS}.")
    p.add_argument("--seed", help="Seed domain(s), comma-separated (e.g. examplemotors.example,examplecycles.example)")
    p.add_argument("--exclude-file", action="append", default=[],
                   help="CSV of companies you already have (any website/domain or email column); they're never "
                        "returned, so they cost nothing. Repeatable.")
    p.add_argument("--exclude-domain", help="Comma-separated domains to exclude")
    p.add_argument("--max-results", type=int, default=25,
                   help="Lookalikes to return (default 25). Each uses a search result, plus 1 per seed found.")
    p.add_argument("--naics", help='Replace the seeds\' NAICS codes (comma-separated, any level, e.g. "3369")')
    p.add_argument("--linkedin-industry", help='Replace the seeds\' LinkedIn industries (";"-separated, exact)')
    p.add_argument("--employee-range", help=f"Replace the seeds' headcount bands (comma-separated: {EMPLOYEE_RANGES})")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--country", help="Replace the seeds' countries (comma-separated full names)")
    where.add_argument("--any-country", action="store_true", help="Don't filter on country")
    p.add_argument("--yes", action="store_true", help="Don't ask after the test search (for scripts)")
    p.add_argument("--output", help="Output CSV (default <seeds file>_lookalikes.csv or lookalikes_<first seed>.csv)")

    args = parser.parse_args()
    if args.mode in ("prospect", "visitors"):
        if not 1 <= args.max_results <= 100:
            parser.error("--max-results must be 1-100")
        if not 0 <= args.founders <= 25:
            parser.error("--founders must be 0-25")
    if args.mode == "visitors" and args.max_companies < 1:
        parser.error("--max-companies must be at least 1")
    if args.mode == "prospect":
        if len(args.company) != len(args.website):
            parser.error("pass exactly one --website for every --company (they are paired in order)")
        if not (args.company or args.companies_file):
            parser.error("provide --company/--website or --companies-file")
        if args.append and args.companies_file:
            parser.error("--append works with --company/--website runs. With --companies-file the results go into "
                         "<file>_with_contacts.csv; re-running is free for lookups made in the last 30 days (cache)")
    if args.mode == "enrich":
        if not (args.prospects_file or args.linkedin or args.name or args.email):
            parser.error("provide --name and --company, --linkedin, --email, or --prospects-file")
        if args.name and not (args.company or args.linkedin or args.email):
            parser.error("--name needs --company (the company's website or domain), --linkedin or --email")
    if args.mode == "accounts" and not 1 <= args.max_results <= 1000:
        parser.error("--max-results must be 1-1000")
    if args.mode == "lookalikes":
        if not (args.seeds or args.seed):
            parser.error("provide --seeds FILE and/or --seed domain,domain")
        if not 1 <= args.max_results <= 1000:
            parser.error("--max-results must be 1-1000")
    return args


def main():
    if not API_KEY:
        print("ERROR: MOLTSETS_API_KEY is not set.")
        print("Add it to a .env file in this directory: MOLTSETS_API_KEY=ms_your_key_here")
        sys.exit(1)
    args = parse_args()
    STATE["refresh"] = getattr(args, "refresh", False)
    {
        "account": run_account,
        "accounts": run_accounts,
        "prospect": run_prospect,
        "enrich": run_enrich,
        "verify": run_verify,
        "visitors": run_visitors,
        "lookalikes": run_lookalikes,
    }[args.mode](args)
    if STATE["cache_hits"]:
        print(f"({plural(STATE['cache_hits'], 'lookup')} answered from the local cache — no cost. "
              "Use --refresh to ask MoltSets again.)")


if __name__ == "__main__":
    main()
