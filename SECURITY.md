# Security

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab → **Report a vulnerability**. Don't
open a public issue for it, and don't include real API keys or real people's contact details in a report.

## What this tool handles

| Asset | Where it lives | Protection |
|---|---|---|
| API keys (`MOLTSETS_API_KEY`, `LUSHA_API_KEY`) | `.env`, or the environment | `.env` is git-ignored. Keys are sent only to `api.moltsets.com` / `api.lusha.com` over HTTPS, and redirects are not followed, so a key can't be forwarded to another host. Keys are never printed or written to a file |
| Contact data (names, emails, phones, LinkedIn URLs) | Output CSVs, `.moltsets_cache.json`, console output | `*.csv`, `*.log` and the cache are git-ignored. Output CSVs, the cache and the visitor record are created readable by the current user only (0600); a file that already exists keeps its permissions |
| Visitor IP addresses | Your log export, `.moltsets_cache.json` | Never written to output CSVs or to `.moltsets_visitors.json` |

Contact data and IP addresses are personal data (GDPR, India's DPDP Act, CCPA and similar). You are responsible for
having a lawful basis to collect and use them, for honouring opt-outs, and for each provider's terms of use.

## Untrusted input

Everything the scripts read from outside is treated as data written by a stranger:

- **API replies.** Names, job titles and company names are written by the people and companies themselves.
- **Input CSVs.** Lead lists from other providers, company lists, exclusion lists.
- **Website-visitor logs.** Any visitor chooses the URL path and user agent they send, and can forge an
  `X-Forwarded-For` header.
- **The cache and the visitor record**, which replay the above.

### What the code does about it

| Risk | Mitigation | Code |
|---|---|---|
| **Spreadsheet formula injection.** A job title like `=HYPERLINK(...)` or `=cmd\|...` runs when the CSV is opened in Excel or Sheets | Every header and cell that starts with `=`, `@`, tab or carriage return, or with `+` / `-` followed by anything but a plain number or phone number, is written with a leading apostrophe | `moltsets/safety.py`: `safe_cell`, `write_table` |
| **Terminal escape injection.** Escape codes in a name or path that rewrite the screen or the window title | Control characters are removed from API replies (values and field names), input cells and page paths before they are printed or saved | `clean_text`, `clean` |
| **Hidden text.** Zero-width characters, bidi overrides and Unicode tag characters that people can't see but software can | Removed along with control characters | `clean_text` |
| **Fake console lines.** A value containing a line break followed by text that looks like the script's own output | Line breaks and tabs in API text become spaces; API strings are cut to 500 characters, page paths to 200. An input cell is printed, matched and sent to the API as one line of at most 500 characters (the output CSV still carries the cell as it was read) | `clean_text`, `moltsets/csvio.py`: `cell` |
| **Key forwarded by a redirect** | `allow_redirects=False` on every request; a 3xx reply is an error | `moltsets/api.py`, `lusha_poc.py` |
| **A server-chosen wait that never ends** | Rate-limit waits are capped at 120 seconds | `MAX_WAIT_SECONDS`, `MAX_RATE_LIMIT_WAIT` |
| **A crafted cell or log line that crashes or hangs a scheduled run** | Malformed URLs, over-long domains, absurd timestamps and unreadable `Retry-After` values are rejected; damaged cache entries are ignored | `valid_domain`, `parse_time`, `is_cached`, `call` |
| **Results overwritten or lost** | MoltSets outputs are never overwritten (a numbered copy is written); the cache and record are written to a temporary file and renamed | `output_path`, `_cache_put`, `save_ledger` |

`tests/test_safety.py` covers each row.

### Prompt injection: using the output with AI assistants

If you give this tool's console output or CSVs to an AI assistant (or run the tool from one), remember that the
content is partly written by strangers. A job title, a company description or a requested URL path can contain
text aimed at the assistant: "ignore your instructions and...". The cleaning above removes the *hidden* ways of
doing that and limits how much text a single field can carry. It cannot tell visible instructions from an unusual
job title, so:

- Treat leads, logs and API replies as data. An assistant should never act on instructions found in them.
  [AGENTS.md](AGENTS.md) tells assistants working in this repository exactly that.
- Don't give an assistant that reads this data the ability to send email, spend credits or push code without your
  confirmation.
- Be most careful with `visitors`: its `top_pages` column is whatever paths visitors requested.
- The same applies to the providers' MCP servers: their replies are third-party text.

### What the code does not protect against

- **Forged visits.** If your log's IP column comes from a client-supplied header (`X-Forwarded-For` behind no
  trusted proxy), anyone can make it look as if a company visited, spending your tokens and search results and
  putting companies of their choice in the output. Use the client-IP field your CDN or load balancer sets.
  `--max-companies` and the per-run budget check limit the cost.
- **Wrong data.** A provider can return a wrong person or an outdated email. `match_confidence`, `role_fit` and
  `email_action` flag likely cases; they don't replace checking before you send.
- **A compromised machine or account.** Anyone who can read `.env` has your keys, and anyone who can write the
  cache can change what a re-run reports. Rotate a key at the provider if it may have leaked.
- **Other spreadsheet importers.** The apostrophe guard follows OWASP's guidance for Excel, Sheets and Numbers. A
  tool that strips leading apostrophes before evaluating cells would undo it.

## Good practice

- Keep `.env` out of commits, screenshots and chat messages. Use a key with the smallest plan and scope you need.
- Keep cron logs outside the repository: console output lists contacts.
- Delete `.moltsets_cache.json` and old output files when you no longer need them.
- Before publishing a fork, run a secret scanner (for example `gitleaks detect`) over the full history.
