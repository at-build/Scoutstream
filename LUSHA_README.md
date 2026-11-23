# Lusha lead search — `lusha_poc.py`

Finds leads (e.g. procurement and supply-chain decision makers) at one or more companies using the
[Lusha v3 API](https://docs.lusha.com/apis/openapi). It filters by role, seniority and location, reveals
contact details for the best matches only (to control credit spend), and exports everything to a CSV.

- Script: [lusha_poc.py](lusha_poc.py)
- Handling of untrusted data, keys and personal data: [SECURITY.md](SECURITY.md)

---

## Contents

1. [How it works](#1-how-it-works)
2. [Setup](#2-setup)
3. [Quick start](#3-quick-start)
4. [Input: companies CSV](#4-input-companies-csv)
5. [Command-line flags](#5-command-line-flags)
6. [Search filters](#6-search-filters)
7. [Pre-search data checks](#7-pre-search-data-checks)
8. [Which prospects get revealed](#8-which-prospects-get-revealed)
9. [Output](#9-output)
10. [Credits and cost](#10-credits-and-cost)
11. [Troubleshooting](#11-troubleshooting)
12. [Implementation notes](#12-implementation-notes)

---

## 1. How it works

Each run goes through these stages. Nothing that costs credits happens until stage 3.

| Stage | What happens | Lusha endpoint | Credits |
|---|---|---|---|
| 1. Filter check | Your `--departments`, `--seniority`, `--location` and `--require-email` terms are matched against Lusha's live filter values | `GET /v3/contacts/prospecting/filters/{departments, seniority, locations, existingDataPoints}` | Free |
| 2. Company check | The companies CSV (and/or `--company` pairs) is validated: columns, company names, websites, per-row locations. Any problems are listed, and you decide whether to continue, fix the file, or quit | `GET .../filters/locations` (per-row locations) | Free |
| 3. Search | One search per company, for people matching the filters | `POST /v3/contacts/prospecting` | 1 credit per 25 results |
| 4. Rank | Prospects are ranked by role fit, then seniority (see [§8](#8-which-prospects-get-revealed)) | — | — |
| 5. Reveal | Emails (and phones with `--phones`) are revealed for the top 5 per company, plus any contacts your account has already revealed (free) | `POST /v3/contacts/enrich` | 1 per email, 5 per phone |
| 6. Export | Every prospect, revealed or not, is written to one CSV | — | — |

---

## 2. Setup

One-time setup, run from the project folder:

```bash
cd scoutstream
pip install -r requirements.txt        # requests, python-dotenv
cp .env.example .env
```

Add your Lusha API key to the `.env` file in the same folder (it is git-ignored; never commit it):

```
LUSHA_API_KEY=your_key_here
```

Requires Python 3.9 or newer (tested on 3.13).

Check your credit balance before a large run:

```bash
python3 -c "import lusha_poc, requests; print(requests.get(lusha_poc.BASE_URL + '/account/usage', headers=lusha_poc._headers()).json()['credits'])"
```

---

## 3. Quick start

**From a CSV (recommended for several companies):**

```bash
python3 lusha_poc.py --companies-file companies.csv --departments procurement --location USA
```

**One or a few companies on the command line** (`--company` and `--website` are paired in order):

```bash
python3 lusha_poc.py \
  --company "Example Appliances" --website exampleappliances.example \
  --company "Example OEM"  --website exampleoem.example \
  --departments procurement --location USA
```

With no other flags, the script searches for **director-and-above** people in **purchasing and supply-chain**
roles, looks at up to **25 prospects per company**, reveals **emails only** for the **top 5**, and writes
`lusha_leads.csv`.

Built-in help: `python3 lusha_poc.py --help`

---

## 4. Input: companies CSV

### Columns

| Column | Required | Contents | Example |
|---|---|---|---|
| `company` | **Yes** | Company name | `Example Appliances` |
| `website` | **Yes** | Company website. Any of `exampleappliances.example`, `www.exampleappliances.example` or `https://exampleappliances.example/` works; it's reduced to the bare domain | `exampleappliances.example` |
| `location` | No | Where the **people** should be based: a country, state, city or continent. Separate several places with `;`. Overrides `--location` for that row; if blank, `--location` applies (or anywhere if not set) | `USA` · `Delhi; Mumbai` |

### Example `companies.csv`

```csv
company,website,location
Example Appliances,exampleappliances.example,USA
Example OEM,https://exampleoem.example,North Carolina
Example Electric,exampleelectric.example,Delhi; Mumbai
Example Wind Group,www.examplewind.example,
```

### Parsing rules

- **Header row:** required. Column names are case-insensitive and can be in any order. Extra columns are ignored.
- **Encoding:** UTF-8, with or without a BOM. In Excel, use **File → Save As → CSV UTF-8**.
- **Blank rows:** skipped silently.
- **Incomplete or invalid rows:** flagged by the pre-search check ([§7](#7-pre-search-data-checks)), not silently dropped.
- **Mixing inputs:** you can pass `--companies-file` and `--company`/`--website` pairs together. File rows come first.

---

## 5. Command-line flags

| Flag | Default | Description |
|---|---|---|
| `--companies-file PATH` | — | CSV of companies to search ([§4](#4-input-companies-csv)) |
| `--company NAME` | — | A company name. Repeat for several companies; each needs a matching `--website` |
| `--website URL` | — | Website for the `--company` in the same position |
| `--departments TERMS` | `purchasing,supply chain` | Comma-separated role terms ([§6.1](#61-roles--departments---departments)) |
| `--seniority TERMS` | `director+` | Comma-separated seniority terms; `all` for every level ([§6.2](#62-seniority---seniority)) |
| `--location PLACE` | anywhere | Where people are based. Repeat for several places. The CSV `location` column overrides it per row ([§6.3](#63-location---location-and-the-csv-location-column)) |
| `--max-results N` | `25` | Prospects to search **per company**. Must be 10–100 (Lusha's page-size limits) |
| `--require-email` | off | Only search for people Lusha has a **work email** for ([§6.5](#65-require-email---require-email)) |
| `--phones` | off | Also reveal phone numbers (5 credits each, versus 1 per email) |
| `--output PATH` | see [§9.1](#91-file-name) | Where to write the output CSV |

You must give either `--companies-file` or at least one `--company`/`--website` pair. A mismatched number of
`--company` and `--website` flags is an error.

---

## 6. Search filters

All filters are applied to **people** (contacts), not to the company. Every term is checked against Lusha before
searching ([§7](#7-pre-search-data-checks)).

### 6.1 Roles / departments (`--departments`)

Lusha has no "Procurement" department; procurement staff sit inside the very broad **Operations** department.
So procurement and supply-chain terms are translated into **job-title keywords**, which Lusha matches against
people's titles:

| Term you pass | Job-title keywords searched |
|---|---|
| `purchasing`, `procurement` | Procurement, Purchasing, Sourcing, Buyer |
| `supply chain`, `supply-chain` | Supply Chain, Logistics, Materials |
| `logistics` | Logistics, Supply Chain |

Any other term is matched against **Lusha's department list** instead. Checked live, the list is: Business
Development, Consulting, Customer Service, Engineering & Technical, Finance, General Management, Health Care &
Medical, Human Resources, Information Technology, Legal, Marketing, Operations, Other, Product, Research &
Analytics, Sales. A department is much broader than a job-title keyword, e.g. `operations` covers everyone in
Operations.

- **Matching:** case-insensitive; a term matches a department if either text contains the other (`sales` → Sales, `engineering` → Engineering & Technical). Use full words: abbreviations like `hr` or `it` won't match.
- **Several terms:** separate with commas: `--departments "procurement,supply chain"`.
- **Unmatched terms:** a term that matches neither a keyword nor a department is flagged. If **no** term matches, the run stops, because there would be no role to search for.

### 6.2 Seniority (`--seniority`)

Lusha's seniority levels (checked live):

| Lusha id | Level | Notes |
|---|---|---|
| 10 | founder | |
| 9 | c-suite | |
| 8 | vice president | |
| 7 | partner | |
| 6 | director | |
| 5 | manager | |
| 4 | senior | Experienced staff without direct reports. Sits **below** manager |
| 3 | entry | |
| 2 | intern | |
| 1 | other | |

Terms you can pass:

| Term | Lusha levels searched |
|---|---|
| `director+` **(default)**, `leadership` | director, partner, vice president, c-suite, founder |
| `executive` | c-suite, founder |
| `manager` | manager |
| `junior` | entry |
| `all` or `any` | every level (no seniority filter) |
| A Lusha level name, e.g. `senior`, `vice president`, `intern` | that level |

- **Combining:** separate with commas, e.g. `--seniority manager,director+`.
- **`senior` is Lusha's level.** It means senior individual contributors, not leadership. For leadership, use `director+` or `leadership`.
- **Unmatched terms:** flagged by the pre-search check. If none match, continuing searches every level.

### 6.3 Location (`--location` and the CSV `location` column)

Where the **person** is based, not where the company is headquartered.

- **What you can pass:** any country, state, city or continent in plain text. Lusha's own location lookup resolves it and handles aliases: `USA` and `US` → United States, `UK` → United Kingdom, `Texas` → Texas, United States, `Europe` → Europe.
- **Ambiguous names:** the script uses Lusha's top match (`California` → the US state, not the city in Colombia) and prints the place it chose. Region codes such as `EMEA` return no match.
- **Several places:** repeat `--location`, or use `;` in the CSV column. A person in **any** of them matches.
- **CSV overrides the flag:** a row's `location` value replaces `--location` for that company. A blank value means `--location` applies.
- **Never searched worldwide by mistake:** a place with no match is dropped. If none of a company's places match, that company is **skipped**, not searched worldwide ([§7](#7-pre-search-data-checks)).

### 6.4 Company matching

Each search is limited to one company, using both its **domain** (from `website`) and its **name**.

### 6.5 Require email (`--require-email`)

Adds Lusha's `existingDataPoints = work_email` filter: only people with a known work email are returned.

| | Default (flag off) | `--require-email` |
|---|---|---|
| Who's in the CSV | Everyone matching the filters | Only people with a work email |
| Who's revealed | Top 5 **with an email on file** (people without one are listed but skipped, see [§8](#8-which-prospects-get-revealed)) | Top 5 |
| Best for | Seeing the whole team, including people to research another way | A clean list where every row can be contacted |

The pre-search check confirms your account offers the `work_email` filter. If it doesn't, this is flagged.

---

## 7. Pre-search data checks

Before any search that costs credits, the script checks the filters and the company list. It lists every problem
it finds and says exactly what continuing would do.

### What's checked

| Check | Problem example | If you continue |
|---|---|---|
| CSV can be read | File missing, not UTF-8 | **Can't continue.** Fix or replace the file |
| Required columns present | No `company` or `website` column | **Can't continue.** Fix or replace the file |
| Each row has a company name | `,orphan.example,` | Row skipped |
| Each row has a website | `No Site Inc,,` | Company skipped |
| Website is a valid domain | `Bad Co,not a website` | Company skipped |
| Row's `location` matches Lusha | `Atlantis` | Company skipped (if none of its places match) or searched only in the places that did match |
| `--location` matches Lusha | `--location Narnia` | Searches only the places that matched. Companies relying on it are skipped if none matched |
| `--departments` terms match | `--departments astrology` | Searches with the terms that matched. If **none** match, the run stops |
| `--seniority` terms match | `--seniority wizard` | Searches with the terms that matched, or every level if none did |
| `--require-email` filter available | Account doesn't offer `work_email` | Searches without it |
| At least one valid company left | Every row invalid | **Can't continue.** Fix or replace the file |

### What you'll see

```
!! Pre-search check found 3 issue(s):
   - line 3 (No Site Inc): missing website — company will be skipped.
   - line 6 (Atlantis Co): no Lusha location matched ['Atlantis'] — company will be skipped.
   - line 7 (Mixed Co): no Lusha location matched ['Narnia'] — searching only ['Texas, United States, North America'].

Continue anyway and search 3 companies? [y/N]
```

- **`y`:** searches the valid companies, with the adjustments listed.
- **`n` (or Enter):** nothing is searched, and the script waits:
  ```
  Search not started. Fix and save companies.csv, or prepare a corrected CSV.
  Press Enter to re-check it, type or drag in the path of a new CSV, or 'q' to quit:
  ```
  - **Enter:** re-checks the same file, e.g. after you've fixed and saved it.
  - **A file path:** checks a different CSV. Dragging a file into the terminal works.
  - **`q`:** quits without searching.

  The check repeats until it passes or you accept the remaining issues.

Notes:

- **Flag problems:** problems with `--departments`, `--seniority`, `--location` or `--require-email` come from the command, not the CSV. Uploading a new CSV won't fix them; quit and re-run with different flags.
- **Companies from the command line:** with `--company`/`--website` and no CSV, the same checks run. Answering `n` simply stops.
- **Unattended runs:** if the script can't ask (piped or scheduled), it stops without searching when any issue is found.
- **No prompt when clean:** if the check finds nothing, the run starts straight away.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Finished, or you chose `q` at the re-check prompt |
| 1 | Stopped before searching: no API key, the filters can't run, declined with no CSV to re-check, or no answer in an unattended run |
| 2 | Invalid command-line flags |

---

## 8. Which prospects get revealed

The search can return up to `--max-results` people per company, but revealing contact details is what costs
credits. So each company's prospects are ranked, and only the best matches are revealed.

### Ranking (best first)

1. **Role fit.** Best if the job title contains a requested keyword (e.g. "Procurement"). Next best if only Lusha's department matches. Worst if neither.
2. **Seniority closeness.** Levels inside the requested range (e.g. anything director-and-above for `director+`) come first. Outside it, the nearer the better.
3. **More senior first.** Among equally good matches: C-suite/founder, then VP, then partner, then director, then manager, and so on.
4. **Ties** keep Lusha's original order.

Seniority comes from Lusha's seniority field. If that's missing, it's read from the job title: "Head of Sourcing"
counts as director, "Managing Partner" as partner, and "Channel Partner Manager" as manager. People whose seniority
can't be determined rank last.

### Reveal rules

| Rule | Detail |
|---|---|
| **Cap: 5 paid reveals per company** | Set by `MAX_REVEALS_PER_COMPANY` |
| **Skip anyone with nothing to reveal** | Search results say what Lusha holds for each person. Anyone with no email on file (no email **or** phone with `--phones`) isn't revealed, and the slot goes to the next-best match |
| **Already-revealed contacts are free** | Contacts your account revealed before cost 0 credits and are revealed **on top of** the cap |
| **Emails by default** | Phones only with `--phones`. The script always tells Lusha which fields to reveal, because Lusha reveals (and bills) both if the request doesn't say |

Everyone not revealed is still written to the CSV, without contact details.

Example: a procurement search, `director+`, 10 prospects:

| Ranked | Prospect | On file | Result |
|---|---|---|---|
| 1 | Chief Procurement Officer | phone only | Skipped: no email (revealed with `--phones`) |
| 2 | VP Procurement | email, phone | Revealed |
| 3–6 | 4 directors | email | Revealed (paid slots 2–5) |
| 7 | 5th director | email | Over the cap: listed, not revealed |
| 8–9 | 2 managers revealed in an earlier run | email, 0 credits | Revealed free |
| 10 | Analyst | nothing | Listed, not revealed |

---

## 9. Output

### 9.1 File name

| Situation | File |
|---|---|
| `--output PATH` given | `PATH` |
| One company | `<company>_lusha_leads.csv`, e.g. `example_appliances_lusha_leads.csv` |
| Several companies | `lusha_leads.csv` |

- The file is written to the folder you run the script from.
- An existing file with the same name is **overwritten**.
- If no prospects are found at all, no file is written.

### 9.2 Columns

One row per prospect, in this order:

| # | Column | Contents |
|---|---|---|
| 1 | `target_company` | Company name from your input, for grouping rows by input company |
| 2 | `full_name` | First and last name |
| 3 | `job_title` | e.g. `Head of Supply Chain` |
| 4 | `job_department` | Lusha department(s), comma-separated, e.g. `Operations` |
| 5 | `seniority` | Lusha seniority, e.g. `director`, `vice president` |
| 6 | `company` | Company name as Lusha lists it (may differ slightly from your input) |
| 7 | `city` | Where the person is based |
| 8 | `country` | Where the person is based |
| 9 | `linkedin` | LinkedIn profile URL |
| 10 | `email` | Revealed email(s), separated by `; ` if more than one. Blank if not revealed |
| 11 | `phone` | Revealed phone(s), separated by `; `. Blank unless `--phones` was used |
| 12 | `data_on_file` | What Lusha holds for this person: `emails`, `phones`, `emails, phones`, blank (nothing), or `unknown` (Lusha didn't say). Useful for deciding whom to reveal later |
| 13 | `contact_revealed` | `True` if contact details were revealed, `False` otherwise |

Reading the last two columns together:

- **`contact_revealed = False` and `data_on_file = emails`:** worth revealing later.
- **`contact_revealed = False` and `data_on_file` blank:** Lusha has nothing to reveal.
- **`contact_revealed = True` with a blank `email`:** rare. Lusha returned nothing for that reveal.

Cells that a spreadsheet would run as a formula (starting with `=`, `@`, or a `+` / `-` that isn't a plain number
or phone number) are written with a leading apostrophe, and Lusha's text has control and invisible characters
removed ([SECURITY.md](SECURITY.md)).

The file is UTF-8 without a BOM. If accented names look garbled in Excel, import it via **Data → From Text/CSV**
and choose UTF-8.

### 9.3 Row order

- **Companies:** in input order, file rows first, then `--company` pairs.
- **Within each company:** revealed prospects first, best match first, then the rest in ranked order.

### 9.4 Console output

For each company, the console shows:
- **The search sent:** role keywords, departments, seniority ids and locations.
- **Counts:** how many prospects were found, how many had nothing to reveal, how many were free re-reveals, and how many were over the cap.
- **Revealed contacts:** name, title, department, seniority, location, LinkedIn and email (plus phone with `--phones`).

Runs with more than one company end with a per-company summary, e.g. `Example Appliances: 18 prospect(s), 5 revealed`.

---

## 10. Credits and cost

Lusha pricing, from `GET /v3/account/usage`:

| Action | Cost |
|---|---|
| Contact search | 1 credit per 25 results returned |
| Reveal email | 1 credit per contact (nothing charged if no email is returned; 0 if already revealed) |
| Reveal phone | 5 credits per contact |
| Filter lookups, pre-search check, account usage | Free |

Maximum cost per company:

| Settings | Search | Reveals | **Max per company** |
|---|---|---|---|
| Defaults (`--max-results 25`, emails) | 1 | 5 | **6 credits** |
| `--phones` | 1 | 30 | **31 credits** |
| `--max-results 100` | 4 | 5 | **9 credits** |

Free re-reveals of contacts your account already paid for come on top of this, at no cost.

**Rate limits:** Lusha's free plan, at the time of writing: 40 credits in total, 100 API requests a
day and 40 a minute. A run makes 2–4 filter lookups, one lookup per distinct location, then one search and one
reveal call per company.

---

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `HTTP 402 ... You've reached your credit limit` | The account is out of credits. Check the balance ([§2](#2-setup)), then add credits or upgrade the plan. Nothing is charged for refused requests |
| `ERROR: LUSHA_API_KEY is not set` | Add `LUSHA_API_KEY=...` to `.env` in the project folder |
| A company shows `search failed` in the summary | The search request was rejected, e.g. out of credits, rate-limited, or `--max-results` outside 10–100. The error is printed above the summary. Other companies still run |
| 0 prospects for a company you'd expect results from | Try loosening filters one at a time: `--seniority all`, remove `--location`, `--departments` with the job-title terms. Also check that the website is the company's main domain; subsidiaries often use their own (e.g. Example Home's US business is Example Appliances, `exampleappliances.example`) |
| A location resolved to the wrong place | Be more specific (a state or country instead of a city name), or check the place printed as "Location:" in the filter check |
| `Can't parse ... save it as 'CSV UTF-8'` | The file isn't UTF-8. Re-save from Excel as **CSV UTF-8** |
| `No answer available (non-interactive run)` | The pre-search check found issues but the script couldn't ask you. Run it in a terminal, or fix the input so the check passes |
| Garbled accented names in Excel | Import the CSV as UTF-8 ([§9.2](#92-columns)) |

---

## 12. Implementation notes

### Lusha endpoints used

| Endpoint | Used for |
|---|---|
| `GET /v3/contacts/prospecting/filters/departments` | Department values (§6.1) |
| `GET /v3/contacts/prospecting/filters/seniority` | Seniority levels and ids (§6.2) |
| `GET /v3/contacts/prospecting/filters/locations?query=...` | Turning place names into location filters (§6.3) |
| `GET /v3/contacts/prospecting/filters/existingDataPoints` | Confirming `work_email` is available (§6.5) |
| `POST /v3/contacts/prospecting` | Searching for people at each company |
| `POST /v3/contacts/enrich` | Revealing emails and phones |
| `GET /v3/account/usage` | Credit balance and pricing (manual check, §2) |

Authentication is the `api_key` HTTP header, taken from `LUSHA_API_KEY`.

### Why not the Lusha MCP server?

Lusha's MCP server (`https://mcp.lusha.com/mcp/claude`) runs the same searches and reveals under the same credit
rules. It's designed for asking Claude or ChatGPT directly in a conversation, not for use from a script. It's fine
for one-off questions, but it lacks this script's safeguards: the reveal cap, emails-only reveals and the
pre-search check. See the [Lusha MCP docs](https://docs.lusha.com/mcp-docs).

### Settings you can change in the script

| Setting | Value | Purpose |
|---|---|---|
| `MAX_REVEALS_PER_COMPANY` | `5` | Paid reveals per company |
| `DEFAULT_MAX_RESULTS` | `25` | Default `--max-results` |
| `DEFAULT_SENIORITY` | `"director+"` | Default `--seniority` |
| `JOB_TITLE_HINTS` | see §6.1 | Role terms and the job-title keywords they search |
| `SENIORITY_KEYWORD_HINTS` | see §6.2 | Seniority terms and the Lusha levels they map to |
| `SENIORITY_LEVELS` | see §8 | The seniority ladder used for ranking |
| `REQUIRE_EMAIL_DATA_POINT` | `"work_email"` | Filter used by `--require-email` |

### What has been checked, and how

| Area | How it was checked |
|---|---|
| Filter lookups (departments, seniority, locations, email data points), account usage | Against the live Lusha API |
| Pre-search checks and prompts, ranking, reveal selection, CSV output | Against a fake (mocked) Lusha API |
| Search and reveal | Worked live in earlier versions. **Not re-checked live** against the latest version |
| "What's on file" and "cost to reveal" data in search results | Taken from Lusha's API spec, not yet seen live. If Lusha doesn't send it, the script falls back to revealing the top 5 by rank, and `data_on_file` shows `unknown` |
