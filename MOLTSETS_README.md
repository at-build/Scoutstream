# MoltSets prospecting — `moltsets_poc.py`

Finds and verifies B2B contacts with the [MoltSets API](https://developer.moltsets.com/): builds target-company
lists, finds relevant decision makers at them, turns website visitors into leads, finds companies
like your best accounts, enriches people you already know, and re-checks
lead files bought from other providers. Every email comes with MoltSets' A–F send-safety grade.

- Script: [moltsets_poc.py](moltsets_poc.py) (code in [moltsets/](moltsets/))
- Handling of untrusted data, keys and personal data: [SECURITY.md](SECURITY.md)

---

## Contents

1. [Modes at a glance](#1-modes-at-a-glance)
2. [Setup](#2-setup)
3. [Quick start](#3-quick-start)
4. [Budget and the pre-run check](#4-budget-and-the-pre-run-check)
5. [`account`](#5-account)
6. [`accounts` — target-company lists](#6-accounts--target-company-lists)
7. [`prospect` — decision makers at target companies](#7-prospect--decision-makers-at-target-companies)
8. [`enrich` — contact details for known people](#8-enrich--contact-details-for-known-people)
9. [`verify` — grade emails, catch job changes, compare providers](#9-verify--grade-emails-catch-job-changes-compare-providers)
10. [`visitors` — website visitors to live leads](#10-visitors--website-visitors-to-live-leads)
11. [`lookalikes` — companies like your best accounts](#11-lookalikes--companies-like-your-best-accounts)
12. [Email risk grades](#12-email-risk-grades)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Modes at a glance

| Mode | You give it | You get | Uses |
|---|---|---|---|
| `account` | — | Plan, tokens, search results left, phone tokens | Free |
| `accounts` | Firmographic filters (industry, size, revenue, country) | A companies CSV ready for `prospect` | Search results |
| `prospect` | Companies (CSV or `--company`/`--website`) | Ranked people in the target roles, with graded emails. With a CSV: your file plus contact columns, one row per contact | Search results + tokens |
| `enrich` | People: LinkedIn URL, email, or name + company domain | Email, title, company, location, personal email | Tokens (+ search for name rows) |
| `verify` | Existing lead CSVs (e.g. `leads/*.csv`) | Email grades, job changes, missing emails, provider comparison | Tokens |
| `visitors` | A website-visitor log export (CSV with an IP column) | The companies that visited, with pages viewed, and the leaders / founders there with graded emails | Tokens (IP lookups) + search results |
| `lookalikes` | Seed domains (your best-fit accounts) | Similar companies you don't have yet, scored 0–100 with the reasons, ready for `prospect` | Search results |

The modes chain: `accounts` / `lookalikes` → `prospect` → `verify`. `visitors` runs on its own, or on a schedule.

---

## 2. Setup

```bash
cd scoutstream
pip install -r requirements.txt        # requests, python-dotenv
pip install -r requirements-dev.txt    # + pytest, to run the tests
python3 -m pytest -q
cp .env.example .env
```

Add your API key (from [app.moltsets.com](https://app.moltsets.com)) to `.env` in the same folder. `.env` is
git-ignored; never commit it.

```
MOLTSETS_API_KEY=ms_your_key_here
```

Python 3.9+ (tested on 3.13). Check the key works and see your balance:

```bash
python3 moltsets_poc.py account
```

---

## 3. Quick start

```bash
# Who are the procurement / supply-chain leaders at Example Space?
python3 moltsets_poc.py prospect --company Example Space --website examplespace.example

# ... and its founders, in the same run (separate search, up to 2)
python3 moltsets_poc.py prospect --company Example Space --website examplespace.example --founders 2

# Same for every company in a CSV, people based in India only
# -> targets_with_contacts.csv: your rows + contact columns, one row per contact
python3 moltsets_poc.py prospect --companies-file targets.csv --location India

# New Indian manufacturing companies (51–500 people) you don't already have
python3 moltsets_poc.py accounts --industry Manufacturing --employee-range 51-200,201-500 \
    --country India --exclude-file targets.csv --exclude-file more_targets.csv

# Contact details for one person
python3 moltsets_poc.py enrich --linkedin https://www.linkedin.com/in/example_some_person
python3 moltsets_poc.py enrich --name "Jane Doe" --company acme.example
python3 moltsets_poc.py enrich --email jane@acme.example

# Re-check leads bought from other providers, side by side
python3 moltsets_poc.py verify leads/lusha_examplemotors_motors_leads.csv leads/targets_procurement_leads.csv
python3 moltsets_poc.py verify leads/crustdata_exampleelectric_leads.csv --expected-company exampleelectric.example

# Which companies visited the website, and who are the procurement leaders and founders there?
python3 moltsets_poc.py visitors --log exports/visits.csv --own-domain example.com

# 25 companies like your best accounts that aren't on your list yet
python3 moltsets_poc.py lookalikes --seed examplemotors.example,examplecycles.example --exclude-file "known_companies.csv"
```

Built-in help: `python3 moltsets_poc.py --help`, and `python3 moltsets_poc.py <mode> --help` per mode.

---

## 4. Budget and the pre-run check

### What things cost

| Allowance | Used by | Free plan | $27 / $97 plans |
|---|---|---|---|
| **Search results** | Each company / person a search returns (`accounts`, `prospect`, `visitors`, `lookalikes`) | **100 lifetime**, max 5 per call | 500 / 7,500 per 5 h; 25 per call |
| **Tokens** | Each lookup that returns data | 1,000 lifetime | Unlimited (fair-use caps) |
| **Phone tokens** | Each mobile number found (`--phones`) | None | Separate subscription |

Misses are always free (no tokens, no search results). They still count toward MoltSets' request limits
(200 search requests / 5 h on the free plan).

Worst case per unit:

| Mode | Per unit | Worst case |
|---|---|---|
| `accounts` | per company returned | 1 search result + 1 token |
| `prospect` | per person returned | 1 search result + 1 token, + 1 token for the email fallback. A company that returns nobody: + 1 search result if it's in MoltSets (free if not) |
| `enrich` | per person | 2–3 tokens. Name rows also make up to 3 search requests; a name-lookup hit uses no search results, and a profile-by-name hit is unconfirmed |
| `verify` | per unique email / LinkedIn URL | 1 token (looked up once even if it appears in several files) |
| `visitors` | per visitor IP, then per company | 1 token per IP matched (free if not). Per company searched: 1 search result for the company lookup + up to `--max-results` + `--founders` (default 6). IPs and companies looked up in the last 30 days come from the cache: free |
| `lookalikes` | per seed, per company | 1 search result per seed found, 1 for the test search, 1 per lookalike returned |

### The pre-run check

Before anything billed runs, every mode:

1. **Validates the input** — CSV readable, required columns present, each row usable (valid domain, LinkedIn
   profile URL, email), duplicates, filter values against MoltSets' fixed vocabularies.
2. **Checks your account** (free) — prints plan, tokens and search results left, and compares them with the
   worst-case cost of the run.

Any problem is listed with what continuing would do:

```
!! Pre-run check found 1 issue(s):
   - This run can use up to 27 search results but 8 are left. Once they run out, remaining searches are skipped
     and the script saves what it has.

Continue anyway with 9 companies? [y/N]
```

- **`y`** runs with the adjustments listed.
- **`n`** runs nothing. For a CSV input it waits: **Enter** re-checks the (fixed) file, a **path** checks a
  different CSV (dragging a file into the terminal works), **`q`** quits.
- **Unattended runs** (piped / scheduled) stop without running anything when there are issues. `visitors` never
  asks: it trims the run to the search results left, and only an unreadable log or `--exclude-file` stops it.
  `lookalikes` asks once more after its test search, so scripts must pass `--yes`.
- A clean check starts the run straight away.

### Running out mid-run

| What runs out | What happens |
|---|---|
| Search results | Remaining searches / name lookups are skipped (marked in the output); LinkedIn and email lookups carry on |
| Tokens / enrichment allowance | The run stops; everything found so far is saved, remaining rows marked `not processed` |
| Phone tokens | Phone lookups stop; the rest continues |
| Short rate limits (< 2 min) | Waited out automatically |

Ctrl+C behaves the same way: what's been found is saved.

When the free search trial reaches 0, the script prints a reminder to upgrade to a paid plan.

### Output files and the cache

- **Spreadsheet-safe CSVs.** A cell that a spreadsheet would run as a formula (it starts with `=`, `@`, or a
  `+` / `-` that isn't a plain number or phone number) is written with a leading apostrophe, in every output file.
  Text from MoltSets and from your input files has control and invisible characters removed
  ([SECURITY.md](SECURITY.md)).
- **No overwriting.** If an output file already exists, the new results go to `<name>_2.csv` (then `_3`, …) and
  the console says so. `prospect --append` merges into the existing file instead. An uploaded companies CSV is
  never modified; its results go to `<file>_with_contacts.csv`.
- **Lookup cache.** Every paid lookup's response (searches, reverse lookups, email and phone lookups) is saved in
  `.moltsets_cache.json` next to the script and reused for 30 days. Re-running a company, changing the ranking or
  merging runs costs nothing; the console reports how many lookups came from the cache. `--refresh` asks MoltSets
  again (and updates the cache). The file holds contact data: it's created readable by you only, and git-ignored. Keep it out of
  shared folders, delete it to clear it. Set `MOLTSETS_CACHE=/path/file.json` to keep it elsewhere.
- **Visitor record.** `visitors` keeps `.moltsets_visitors.json` (domains only, no IPs): which companies it has
  seen, when each was searched, their visit figures, and which are held over for the next run
  ([§10](#the-record-file)). Delete it to start fresh.

---

## 5. `account`

```bash
python3 moltsets_poc.py account
```

Prints plan and status, token balance, search results left (and max per call), each fair-use window with its
reset time, phone tokens, whether US personal-email lookups are available, and today's usage. Free.

---

## 6. `accounts` — target-company lists

Searches MoltSets' company database (`search_companies`) and writes a CSV that `prospect` reads directly.

### Flags

| Flag | Description |
|---|---|
| `--industry` | MoltSets' 22 industry buckets, comma- or `;`-separated, e.g. `Manufacturing`, `Automotive`, `Energy`, `Information Technology`. Case-insensitive; invalid values are flagged and dropped |
| `--linkedin-industry` | LinkedIn industry labels, `;`-separated, exact (e.g. `Aviation and Aerospace Component Manufacturing`). Finer than `--industry` |
| `--naics` | NAICS codes, any level (`336` transport equipment, `3364` aerospace parts) |
| `--employee-range` | `1-10`, `11-20`, `21-50`, `51-200`, `201-500`, `501-1000`, `1001-5000`, `5001+` |
| `--revenue-range` | `Below $500k`, `$500k - $1M`, `$1M - $5M`, `$5M - $10M`, `$10M - $20M`, `$20M - $50M`, `Above $50M` |
| `--country`, `--state` | Company location (full English country names; `USA`/`UK` translated) |
| `--name` | Free-text company-name search |
| `--domain` | Exact domain(s) — a firmographic lookup of companies you already know |
| `--exclude-file PATH` | Suppression list: any CSV with a website/domain (or email) column. Repeatable. Excluded companies are never returned and cost nothing |
| `--exclude-domain` | Comma-separated domains to exclude |
| `--max-results N` | Companies to return (default 5). Pages automatically |
| `--location` | Value for the output's `location` column (default: `--country`) |
| `--output` | Default `moltsets_accounts.csv` |

Lists in one flag are OR-ed; different flags are AND-ed (`--industry Manufacturing,Automotive --country India` =
Manufacturing **or** Automotive, **and** in India).

**Caveats:** company location is where MoltSets thinks the team is based — multinationals sit under one office.
Industry buckets can be off (Example Space is filed under "Government and Public Administration"); use
`--linkedin-industry` or `--naics` for niches.

### Output columns

`company`, `website`, `location` (the three `prospect` reads), then `domain`, `industry`, `linkedin_industry`,
`naics`, `employee_range`, `employee_count`, `revenue_range`, `city`, `state`, `country`, `linkedin_url`,
`moltsets_score`.

---

## 7. `prospect` — decision makers at target companies

The MoltSets equivalent of `lusha_poc.py`: `search_people` per company, filtered by role, seniority and location
(leaders first, then people with no level on record, optionally founders — see
[How the paid slots are filled](#how-the-paid-slots-are-filled)). Results already include each person's business
email and grade, so there's no separate reveal step.

### Input

Either `--companies-file` or `--company` / `--website` pairs (or both):

| Column | Required | Contents |
|---|---|---|
| `website` (or `domain`, `company_domain`) | **Yes** | Any of `examplespace.example`, `www.examplespace.example`, `https://www.examplespace.example/` |
| `company` | No | Name, for labelling (defaults to the domain) |
| `location` (or `country`) | No | Country where the **people** should be based; several with `;`. Overrides `--location` for the row |

`targets.csv`, `more_targets.csv` and the `accounts` output all work as-is.

### Flags

| Flag | Default | Description |
|---|---|---|
| `--departments` | `purchasing,supply chain` | Roles, matched as job-title keywords (see below). `all` = any title |
| `--seniority` | `director+` | See below. `all` = any level |
| `--founders N` | `0` (off) | Also run a **separate** search for up to N founders / CEOs per company |
| `--location` | anywhere | Country of the people, full English name (`India`, `United States`; `USA`/`UK`/`UAE` translated). Repeatable |
| `--ignore-location-column` | off | Don't treat the CSV's `location` / `country` column as where people are based. Use it when that column is the company's HQ: filtering on it misses staff abroad, and values like `Finland / Poland` match nothing. The column is then used as the HQ for `match_confidence` (`/` or `;` separate several) |
| `--max-results N` | `5` | People per company for the role search (1–100). Each uses a search result |
| `--no-email-fallback` | off | Skip the best-email lookup for people returned without an email |
| `--phones` | off | Also look up mobile numbers (phone tokens) |
| `--output` | With `--companies-file`: `<file>_with_contacts.csv` next to it ([below](#output-with-a-companies-csv-your-file-plus-contact-columns)). Otherwise `<company>_moltsets_leads.csv` (one company) or `moltsets_leads.csv` | Never overwritten: a numbered copy is written instead (see [§4](#output-files-and-the-cache)) |
| `--append` | off | `--company`/`--website` runs only. Merge into `--output` if it exists: same person = same LinkedIn URL, or same name at the same company; a newer row replaces an older one unless it has no email |
| `--refresh` | off | Ignore cached lookups and ask MoltSets again |

**Roles.** MoltSets has no procurement department, so roles are searched by job title (filled for ~85% of
profiles, vs ~60% for departments). Known terms expand; anything else is used as typed (e.g. `"plant head"`):

| Term | Title keywords (any can match) |
|---|---|
| `purchasing`, `procurement` | Procurement, Purchasing, Purchase, Sourcing, Buyer |
| `supply chain` | Supply Chain, Logistics |
| `logistics` | Logistics, Supply Chain |
| `materials` | Materials — opt-in: catches "Materials Manager" roles common in Indian manufacturing, but at hardware / aerospace companies mostly returns materials engineers |
| `founder`, `founders` | Founder (matches Co-Founder too) — prefer `--founders N` |

**Seniority.** MoltSets levels: Intern, Entry, Senior, Manager, Director, Head, VP, Partner, Owner, C Suite.

| Term | Levels searched first |
|---|---|
| `director+` **(default)**, `leadership` | Director, Head, VP, Partner, Owner, C Suite |
| `vp+` | VP, Partner, Owner, C Suite |
| `executive`, `c-suite`, `founder` | C Suite, Owner |
| `director` | Director, Head |
| `manager+` / `manager` | Manager and up / Manager |
| `junior` | Intern, Entry |
| a level name, e.g. `VP` | that level |

### How the paid slots are filled

MoltSets decides who comes back from a search, and its order doesn't put leaders first. So each company gets
up to three searches, each using only the slots it fills:

| Step | Search | Slots |
|---|---|---|
| 1 | Title keywords **+ the wanted seniority levels** | up to `--max-results` |
| 2 | Title keywords + **no seniority on record** (all ten levels excluded, so nobody from step 1 comes back) | whatever step 1 left |
| 3 | `--founders N` only: titles Founder / CEO / Chief Executive, any level | up to N |

Step 2 matters because ~40% of profiles have no level: a "Head of Procurement" with nothing on record would
never pass a level filter. With `--seniority all` there's a single search instead of steps 1–2. Searches that
return nobody are free.

### Checks on every person

| Column | Values | Meaning |
|---|---|---|
| `role_fit` | `strong` / `weak` | `weak`: an assistant or intern title ("Executive Assistant to the Co-Founder"), an engineer matched only on `materials`, a title with none of the keywords, or a non-founder title from the founder search |
| `match_confidence` | `high` / `medium` / `low` | Is this person really at the company? **High** whenever they have a work email on the company's domain, wherever they're based. Otherwise **low** if the listed employer has another domain, or they're in the wrong country: outside the company's HQ country (from the CSV, with `--ignore-location-column`) and not where most of its people are; without an HQ, in a country none of its other people share (e.g. a "CEO" on another continent from everyone else). **Medium** if they're in the country where most of its people are but not the HQ, if their name contains the company's name (a company page, or a founder like Jane Doe of Jane Doe Foods), or if there's no location or email to confirm |
| `contact_type` | `founder` / `role` | Founder / CEO title, or the role search |

Weak and low-confidence people are kept but ranked last, and get **no** email-fallback or phone lookup, so
no tokens are spent on them. The reason is in `notes`.

**Ranking** (best first): match confidence → role fit → founders before role contacts → title contains a
requested keyword → closest to the wanted seniority → more senior → MoltSets' match score. Seniority is read
from the title first ("Chief …" / "Founder" = C Suite, "Head" / "Director" / "General Manager" = Director,
"Sr …" = Senior), then from MoltSets' seniority field, which is often wrong (a "Sourcing Coordinator" came back as
C Suite). Duplicate profiles (same LinkedIn URL or name) are merged, keeping the one with an email.

**Email fallback.** People returned without an email get a `linkedin_to_best_email` lookup (work email, else
personal) — tokens only, free when nothing is found.

**Email checks.** Every email is checked against the company's domain ([§12](#12-email-risk-grades)): personal
inboxes are labelled `personal`; university addresses (`.edu`, `.ac.in` …) and other companies' addresses get
`email_action = verify first (…)` whatever their grade. A domain that two or more of the company's people email
from counts as the company's own (e.g. website acme.example, email
acme-aero.example); the console prints `Also emails from: …`.

**Nobody found?** The script checks whether the company is in MoltSets at all (a company lookup by domain:
free if it isn't, 1 search result if it is) and says which:

```
No matches: examplegamma.example isn't in MoltSets — check the website (another domain?) or use another provider.
No matches: Example Beta is in MoltSets (11-20 employees, India) but nobody matches these roles — try --departments all, ...
```

### Output with a companies CSV: your file plus contact columns

With `--companies-file`, the results come back **in your own spreadsheet**: a copy of it,
`<file>_with_contacts.csv`, saved next to the original (which is never changed).

- **Every column and row you uploaded is kept,** in the same order, with the contact columns added on the right.
- **One row per contact.** A company with 6 contacts appears on 6 rows, its own columns repeated on each, so you
  can filter and sort as usual.
- **One row for a company with no contacts,** contact columns blank. `contact_search_status` says why.
- **Flagged contacts are included,** after the good ones; filter on `contact_role_fit` / `contact_match_confidence`.

```
Company,Website,Segment,contact_search_status,...,contact_full_name,contact_job_title,contact_email,...
Acme Robotics,acme.example,Robotics,"8 contacts found, 7 with email, 4 flagged",...,Jane Doe,Co-Founder and COO,jane@acme.example,...
Acme Robotics,acme.example,Robotics,"8 contacts found, 7 with email, 4 flagged",...,John Roe,Head of Supply Chain,john@acme.example,...
...
Example Gamma,examplegamma.example,EV,no contacts: examplegamma.example isn't in MoltSets — check the website ...,,,,...
```

`contact_search_status` values:

| Status | Meaning |
|---|---|
| `N contacts found, M with email[, K flagged]` | Searched; one row per contact follows |
| `no contacts: <domain> isn't in MoltSets …` | The company isn't covered — check the website or use another provider |
| `no contacts: <company> is in MoltSets … but nobody matches these roles …` | Covered, but no one in these roles — broaden `--departments` / `--seniority`, or add `--founders` |
| `not searched (search allowance used up)` / `not searched (run stopped)` | Re-run once the allowance is back; finished companies come from the cache, free |
| `N contacts found before the run stopped (…)` | Partial result for the company being searched when the run stopped |
| `search failed: …` | MoltSets error for that company |
| `skipped: …` | Row not searched: invalid or missing website, or the same website as an earlier row (whose rows list the contacts) |

Contact columns are every lead column below, prefixed `contact_` so they can't clash with yours
(`contact_full_name`, `contact_email`, `contact_email_action`, …), plus `contact_search_status`,
`contact_search_company` and `contact_search_domain` (the company name and domain as searched). Blank rows in
your file are dropped. Companies added with `--company`/`--website` in the same run go after your file's rows.

**Re-running:** pass the original file again, or the `_with_contacts` file itself. Its old contact columns are
dropped, and the next copy is saved as `_with_contacts_2.csv`. Lookups made in the last 30 days come from the
cache, so a re-run only pays for what's new (`--refresh` to ask MoltSets again).

### Output columns (lead list)

Runs with only `--company` / `--website` write a plain lead list, one row per contact:

`target_company`, `target_domain`, `rank`, `contact_type`, `role_fit`, `match_confidence`, `full_name`,
`job_title`, `seniority`, `department`, `company`, `city`, `state`, `country`, `linkedin_url`, `email`,
`email_type` (business / personal / academic), `email_risk_score`, `email_action`, `email_validated_at`,
`email_source` (people search / best-email lookup), `mobile_phone`, `mobile_phone_validated_at`,
`role_start_date`, `moltsets_score`, `notes`.

Examples:

```bash
# Contacts added to your own sheet: writes targets_with_contacts.csv next to targets.csv
python3 moltsets_poc.py prospect --companies-file targets.csv --founders 2

# A couple of companies, added to an existing lead list
python3 moltsets_poc.py prospect --company "Acme Robotics" --website acme.example --founders 2 \
    --output leads/procurement_founder_leads.csv --append
```

---

## 8. `enrich` — contact details for known people

### Input

One person by flags — `--linkedin URL`, `--email ADDRESS`, or `--name NAME --company DOMAIN` — or a CSV via
`--prospects-file` (both can be combined). CSV headers are case-insensitive:

| Column | Accepted headers |
|---|---|
| LinkedIn URL | `linkedin_url`, `linkedin`, `linkedin_profile` |
| Email | `email`, `business_email`, `work_email` |
| Name | `name`, `full_name`, or `first_name` + `last_name` |
| Company domain | `company_domain`, `domain`, `website`, or `company` when it holds a domain |

Each row needs a LinkedIn URL, an email, or a name + company domain (a company *name* alone isn't enough).
Duplicates are skipped.

### Lookup order

| Row has | Lookups |
|---|---|
| LinkedIn URL | `reverse_linkedin_lookup` → `linkedin_to_best_email` if no email |
| Email (no LinkedIn) | `reverse_email_lookup` → (best email via the LinkedIn it returns, if needed) |
| Name + domain only | free `count_only` check (skips the rest if nobody matches; flags if several profiles match) → `search_business_email_by_name` → `search_business_profile_by_name` if no LinkedIn found → as for LinkedIn |

`--phones` adds `linkedin_to_mobile_phone` for anyone with a LinkedIn URL.

### Output columns

`input_name`, `input_company`, `input_linkedin`, `input_email`, `full_name`, `job_title`, `seniority`,
`department`, `company`, `company_domain`, `company_industry`, `company_size`, `city`, `state`, `country`,
`linkedin_url`, `email`, `email_type`, `email_risk_score`, `email_action`, `email_validated_at`, `email_source`,
`personal_email`, `personal_email_risk_score`, `mobile_phone`, `mobile_phone_validated_at`, `lookup_status`,
`notes`.

`lookup_status`: `email found` · `profile found, no email` · `not found` · `not looked up` (search allowance used
up) · `error` (see `notes`) · `not processed` (run stopped).

Default file: `<prospects file>_moltsets_contacts.csv`, or `<name>_moltsets_contacts.csv` for one person.

---

## 9. `verify` — grade emails, catch job changes, compare providers

Re-checks lead files you already have — typically `leads/*.csv` from Lusha, Crustdata, Explorium, RocketReach
or Adapt — using tokens only (no search results).

```bash
python3 moltsets_poc.py verify leads/*.csv
python3 moltsets_poc.py verify leads/crustdata_exampleelectric_leads.csv --expected-company exampleelectric.example --limit 10
```

### Input

Any CSV with an **email** and/or **LinkedIn** column (same header names as `enrich`). Optional: a `company` /
`target_company` and `website` column (the employer the lead should be at), `job_title` (to detect title
changes), `full_name`.

| Flag | Description |
|---|---|
| `--expected-company NAME_OR_DOMAIN` | Employer every lead should be at, for files without a company column (e.g. Crustdata's). Without it, the job-change check falls back to the email's domain, or says `unknown` |
| `--limit N` | Only check the first N rows of each file — try it cheaply first |
| `--output-dir DIR` | Where to write results (default: current folder) |

### What's checked per lead

1. **Email grade** — each email (up to 3 per cell) goes to `reverse_email_lookup`. If MoltSets has that exact
   address on file, its A–F grade is recorded.
2. **Current employer and title** — from the same lookup, or `reverse_linkedin_lookup` for leads without an email.
   Compared with the expected employer (domains by organisation part, names ignoring Ltd/India/Pvt etc.).
   A title counts as changed only if fewer than half its words are shared.
3. **Missing emails** — MoltSets' work email is added when the lead had none (or has moved). If that email's
   domain doesn't match the person's current employer, it's flagged as likely outdated.

### Output

`<file>_moltsets_verified.csv` per input: the original columns plus

| Column | Contents |
|---|---|
| `ms_status` | Headline result (below) |
| `ms_still_at_company` | `yes` / `no` / `unknown` |
| `ms_current_company`, `ms_current_domain`, `ms_current_title` | Where MoltSets says they are now |
| `ms_title_changed`, `ms_role_start_date` | |
| `ms_input_email_grade`, `ms_input_email_action`, `ms_input_email_validated_at` | Grade of the provider's email |
| `ms_work_email`, `ms_work_email_grade` | Work email MoltSets has that the input didn't |
| `ms_personal_email`, `ms_linkedin_url`, `ms_notes` | |

`ms_status`, most important first:

| Status | Meaning / action |
|---|---|
| `moved company` | Left the target account — find a replacement there; the person is a warm lead at `ms_current_company` |
| `email: do not send (D)` | Spam trap / complaint risk — suppress |
| `email not confirmed` | MoltSets knows the person but not that email — treat as unverified |
| `different email on file` | MoltSets has another work email (`ms_work_email`) — prefer it |
| `email doesn't match employer` | MoltSets' own email is for a different company — don't use it |
| `new email found` | Lead had no email; MoltSets found one |
| `title changed` | Same company, different role — re-check fit |
| `ok` | Still there, same role, email fine |
| `not found in MoltSets` | No data — can't verify |
| `not checked` / `not processed` | No email/LinkedIn in the row / run stopped |

**Provider comparison.** The console prints one row per file (provider inferred from the file name); with several
files it's also saved as `moltsets_provider_comparison.csv`: leads, leads with email, grades A–F,
`pct_sendable_A_B` (provider emails graded A/B ÷ leads with an email), `new_emails_from_moltsets`,
`moved_company`, `title_changed`, `not_in_moltsets`.

---

## 10. `visitors` — website visitors to live leads

Turns an export of your website's logs into contacts at the companies that visited.

```bash
python3 moltsets_poc.py visitors --log exports/visits.csv --own-domain example.com
```

### Input

A CSV export from your server, CDN or host (Cloudflare, nginx, a Vercel Log Drain, ...). Headers are matched
case-insensitively. Vercel's dashboard log export has no IP column; use a Log Drain, or a middleware that writes time,
path, user-agent and the first `x-forwarded-for` address to a CSV.

| Field | Accepted headers | Required |
|---|---|---|
| IP | `ip`, `ip_address`, `client_ip`, `ClientIP`, `remote_addr`, `remote_ip`, `c-ip`, `X-Forwarded-For` (first address) | yes |
| Time | `timestamp`, `time`, `datetime`, `date`, `EdgeStartTimestamp` | no |
| Page | `url`, `page`, `page_url`, `path`, `request_uri`, `ClientRequestPath` | no |
| User agent | `user_agent`, `ua`, `ClientRequestUserAgent` | no |

Skipped, and counted at the start of the run:
- bots, by user agent;
- private and reserved addresses;
- IPv6 addresses, since MoltSets resolves IPv4 only.

Requests for `.js`, `.css`, images and fonts keep the IP but don't count as page views.

### What it does

1. **Resolves each IP** to a company with `ip_to_company`: 1 token per match, misses free, cached for 30 days.
   When there are several candidates, the highest match percentage wins. The busiest IPs go first, and the uncached
   ones are capped so they never use up the enrichment records this window: it keeps (`--max-companies` or the
   number of IPs, whichever is lower) × (`--max-results` + `--founders`) records for the contacts' email lookups
   (none with `--no-email-fallback`). The IPs over the cap are listed and resolved next run, if the next export
   still has them. If MoltSets stops the resolving anyway, the companies resolved so far are still searched.
2. **Adds up the visits per company:** IPs, page views, top pages, first and last seen.
3. **Ignores** internet providers, telecoms, clouds and proxies (Jio, Airtel, AWS, Google, Cloudflare, Zscaler, ...),
   personal-mail and university domains, your `--own-domain`, and anything in `--exclude-file` / `--exclude-domain`.
4. **Adds back held-over companies** from earlier runs that this log no longer has, with the visit figures kept in
   the record file. So `--log` doesn't have to be a cumulative export.
5. **Searches in this order:** companies held over from earlier runs, then companies never searched, each by most
   page views and then most recent visit. For each company, a company lookup (1 search result) gives its name,
   industry, size and HQ, and then the same people search and checks as `prospect` run.
6. **Re-reads companies searched in the last 30 days** from the cache. They cost nothing, so they don't count
   towards `--max-companies` or the budget, and every run's CSV lists their contacts. `--refresh` turns this off.
7. **Never asks a question.** If the search results left this window can't cover the other companies at worst-case
   cost, it searches the ones that fit and holds the rest over to the next run. A run stopped by Ctrl+C or MoltSets
   does the same, and saves the CSV and the record file whatever stopped it.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--log` | — | The log export (required) |
| `--own-domain` | — | Your website's domain(s); visits from them are ignored. Without it, the run prints a note |
| `--max-companies` | 50 | Most companies searched per run (companies re-read from the cache don't count) |
| `--max-results`, `--founders` | 3, 2 | Role contacts and founders per company, as in `prospect` |
| `--departments`, `--seniority`, `--location`, `--no-email-fallback` | as in `prospect` | |
| `--exclude-file`, `--exclude-domain` | — | Companies to ignore (customers, competitors). An unreadable file, or one without a website / domain or email column, stops the run before any lookup |
| `--output` | `visitor_leads_<date>.csv` | Never overwritten |

### Output

One row per contact, and one row with a status for a company with none.

| Column | Content |
|---|---|
| `visitor_company`, `visitor_domain`, `visitor_industry`, `visitor_employee_range`, `visitor_country` | The visiting company |
| `ip_match_pct`, `visitor_ip_count`, `visitor_notes` | MoltSets' match percentage, distinct IPs, and `weak IP match` below 50% |
| `page_views`, `top_pages`, `first_seen`, `last_seen` | What they looked at, and when |
| `new_company` | `yes` until the company has been searched once: a company held over is still `yes` in the run that finds its contacts |
| `contact_search_status`, `contact_*` | As in `prospect`'s `_with_contacts` output |

IP addresses are never written to the output. Page paths are whatever a visitor requested, so they are cleaned
of control characters and cut to 200 characters ([SECURITY.md](SECURITY.md)).

### Running it on a schedule

```cron
0 */6 * * * cd "/path/to/scoutstream" && python3 moltsets_poc.py visitors --log exports/latest.csv --own-domain example.com >> "$HOME/Library/Logs/moltsets-visitors.log" 2>&1
```

Point `--log` at your latest export, cumulative or just the latest period. IPs and companies looked up in the last
30 days come from the cache, so re-running costs nothing for them. Companies held over go first next time, even if
the next export doesn't have them. The log goes outside the repo because it lists contacts (see Privacy).

### The record file

`.moltsets_visitors.json` holds one entry per visiting company, keyed by domain. It never holds IP addresses.

| Field | Meaning |
|---|---|
| `first_seen_run`, `last_run` | The first and latest runs that saw the company |
| `last_visit` | Its latest visit in any log |
| `pending` | Held over: searched first next run |
| `first_searched_run`, `last_searched_run` | The first and latest runs whose search of it finished (contacts found, none found, or not in MoltSets). A `last_searched_run` in the last 30 days means its lookups are cached |
| `stats` | This run's visit figures: page views, number of IPs, top 5 pages with counts, first and last seen, IP match % |

### Privacy

IP addresses count as personal data (GDPR, India's DPDP Act). They stay only in your log file and in
`.moltsets_cache.json`. Both are git-ignored, so keep them out of shared folders too. Your website's privacy notice
should say that visiting companies are identified from IP addresses. The console output, and so any cron log, lists
contacts' names, emails and LinkedIn URLs: keep it outside the repo (`*.log` is git-ignored as a fallback).

---

## 11. `lookalikes` — companies like your best accounts

```bash
python3 moltsets_poc.py lookalikes --seeds best_accounts.csv --exclude-file "known_companies.csv"
python3 moltsets_poc.py lookalikes --seed examplemotors.example,examplecycles.example --max-results 10
```

1. **Looks up each seed** by domain: 1 search result each if MoltSets has it, cached. Seeds MoltSets doesn't have are
   listed, at no cost.
2. **Builds a profile** of what the seeds share: NAICS codes (exact, and 4-digit), LinkedIn industries, headcount
   bands plus one band either side, and countries. `--naics`, `--linkedin-industry`, `--employee-range` and
   `--country` / `--any-country` replace the matching part.
3. **Runs a test search** (1 search result) that shows how many companies match, then asks before fetching more.
   `--yes` skips the question.
4. **Searches in tiers:** exact NAICS + LinkedIn industry first, then 4-digit NAICS to fill the remaining slots. Seeds
   and your `--exclude-file` lists are excluded in the request, so they cost nothing. MoltSets takes at most 100
   exclusions per search: with a longer list, the rest are filtered here, each one that comes back costs a search
   result, and the tier is fetched again to refill the slot. The run says how many that was.
5. **Scores** every result from 0 to 100 against its closest seed:

| Signal | Points |
|---|---|
| NAICS: shares a 6- / 4- / 3- / 2-digit code | 40 / 30 / 15 / 5 |
| Shares a LinkedIn industry | 25 |
| Same headcount band / one away | 15 / 8 |
| Same revenue band / one away | 10 / 5 |
| Same country | 10 |

| Flag | Default | Meaning |
|---|---|---|
| `--seeds` / `--seed` | — | A CSV of seed companies (any website / domain column), and / or seed domains, comma-separated. Up to 100 seeds |
| `--exclude-file`, `--exclude-domain` | — | Companies you already have; the first 100 are excluded in the request (free), the rest filtered locally |
| `--max-results` | 25 | Lookalikes to return |
| `--naics`, `--linkedin-industry`, `--employee-range` | from the seeds | Replace that part of the seed profile |
| `--country` / `--any-country` | the seeds' countries | Replace the countries, or don't filter on country |
| `--yes` | — | Don't ask after the test search. Needed for scripts and cron |
| `--output` | `<seeds file>_lookalikes.csv` | Never overwritten |

Output: `<seeds file>_lookalikes.csv` (or `lookalikes_<first seed>.csv`), in the `accounts` columns plus
`similarity_score`, `most_similar_seed`, `match_reasons` and `tier`. Then run:
`python3 moltsets_poc.py prospect --companies-file <that file> --ignore-location-column`.

---

## 12. Email risk grades

MoltSets grades **send safety** (will the mailbox accept mail without bouncing or hitting a spam trap) — not
whether the address belongs to the person.

| Grade | Meaning | `email_action` |
|---|---|---|
| A | Validated deliverable | send |
| B | Known engagement (past open / click) | probably send |
| C | Catch-all domain — can't be determined | your call (catch-all domain) |
| D | Spam trap, abuse, do-not-mail or recent complaint | **do not send** |
| F | No signal either way (also used when no grade comes back) | your call (no signal) |

A grade says nothing about *whose* inbox it is. So, whatever the grade, the script also checks the domain:

| Email | `email_type` | `email_action` |
|---|---|---|
| Gmail, Hotmail, Yahoo, Outlook, iCloud, Rediffmail … (any country domain) | `personal` | per grade |
| University address (`.edu`, `.ac.<country>`, `.edu.<country>`) | `academic` | `verify first (university address)` |
| Another company's domain (e.g. a previous employer) | as returned | `verify first (not a <company> address)` |

Seen in testing: a CEO's university alumni address and another CEO's previous-company address, both graded A.

Suppress D. Send C and F at lower volume, separately from A/B. Emails are re-validated on a 90-day cycle;
`*_validated_at` can be blank for catch-all domains.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `MOLTSETS_API_KEY is not set` | Add it to `.env` in the folder you run from |
| `unauthorized` / 401 | Key wrong or revoked — create a new one at app.moltsets.com |
| 403 `forbidden` | Missing User-Agent — the script always sends one; only seen if calling the API by hand |
| `fair_use_limit_exceeded` with no wait time | Free search trial used up — upgrade to a paid plan |
| `fair_use_*` with a wait time | 5-hour window used up — the script waits if it's under 2 minutes, otherwise saves and stops; re-run after the reset time shown by `account` |
| `prospect` finds 0 people | The console says whether the company is in MoltSets. If it isn't, check the website's domain or use another provider. If it is, try `--seniority all`, broader `--departments`, `--founders 2`, or drop `--location`. Zero-result searches are free |
| Rows flagged `low` match confidence | MoltSets attached an unrelated profile to the domain — check the LinkedIn profile before using it |
| `... already exists — saving to ..._2.csv` | By design: results are never overwritten. Use `--append` (prospect) to merge |
| Re-run gave the same results without spending | Answered from the cache; add `--refresh` for fresh data |
| `accounts` returns nothing | A filter value is off — e.g. a `--linkedin-industry` label not spelt exactly. Try `--industry` or a shorter `--naics` |
| `--phones` says no phone tokens | Phone tokens are a separate paid subscription |
| `internal_error` on a row | MoltSets endpoint briefly unavailable — retried once, then noted on the row; re-run later |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Finished, or `q` at the re-check prompt |
| 1 | Stopped before running: no API key, account problem, declined with nothing to re-check, or no answer in an unattended run |
| 2 | Invalid command-line flags |
