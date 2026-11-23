# Scoutstream

Command-line B2B prospecting on top of two data providers, [MoltSets](https://developer.moltsets.com/) and
[Lusha](https://docs.lusha.com/apis/openapi). Give it companies, people or a website-visitor log; get ranked
decision makers with graded emails in a CSV, with a cost check before anything billed runs.

It is a proof of concept: two scripts, no service to deploy, your own API keys.

## What it does

| Script | Mode | You give it | You get |
|---|---|---|---|
| `moltsets_poc.py` | `account` | — | Plan, tokens and search allowance left (free) |
| | `accounts` | Firmographic filters | A target-company CSV |
| | `prospect` | Companies | Ranked people in the roles you want, with graded emails |
| | `enrich` | People (LinkedIn URL, email, or name + company) | Email, title, company, location |
| | `verify` | Lead CSVs from any provider | Email grades, job changes, a provider comparison |
| | `visitors` | A website-visitor log (CSV with an IP column) | The companies that visited and the leaders there |
| | `lookalikes` | Your best-fit accounts | Similar companies, scored 0–100 |
| `lusha_poc.py` | — | Companies | People in the roles you want; contact details revealed for the top 5 per company |

The default roles are procurement and supply-chain leaders; `--departments` and `--seniority` change that.

## Quick start

Python 3.9 or newer.

```bash
git clone <this repository> scoutstream && cd scoutstream
pip install -r requirements.txt
cp .env.example .env          # then add your key(s); .env is git-ignored
```

```bash
python3 moltsets_poc.py account                                   # free: checks the key
python3 moltsets_poc.py prospect --company "Acme Robotics" --website acme.example --founders 2
python3 moltsets_poc.py prospect --companies-file targets.csv     # -> targets_with_contacts.csv
python3 lusha_poc.py --companies-file companies.csv --departments procurement --location USA
```

Every mode except `account` spends provider credits. Each one validates its input and checks your balance first,
lists any problem with what continuing would do, and asks before going over what's left.

## Documentation

- [MOLTSETS_README.md](MOLTSETS_README.md): every MoltSets mode, flag and output column
- [LUSHA_README.md](LUSHA_README.md): the Lusha script
- [MOLTSETS_SPEC.md](MOLTSETS_SPEC.md): why it's built this way, endpoint map, budget model, roadmap
- [SECURITY.md](SECURITY.md): keys, personal data, untrusted input, use with AI assistants
- [AGENTS.md](AGENTS.md): rules for AI coding assistants working in this repository

## Layout

```
moltsets_poc.py        MoltSets command line (argument parsing only)
moltsets/              one module per concern (api, csvio, people, companies, safety) and per mode (modes/)
lusha_poc.py           Lusha script (single file; uses moltsets/safety.py)
tools/offline_replay.py  re-run a MoltSets command from the local cache only, spending nothing
tests/                 pytest suite against a fake API
```

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

The tests never call a real API.

## Data and privacy

The output contains personal data: names, work emails, sometimes phone numbers. Output CSVs, logs, the lookup
cache (`.moltsets_cache.json`) and `.env` are git-ignored; keep it that way. You are responsible for using the data
lawfully (GDPR, DPDP, CAN-SPAM and similar) and within each provider's terms. See [SECURITY.md](SECURITY.md).

This project is not affiliated with MoltSets or Lusha.

## Examples use placeholders

Every company, person, email and profile URL in the documentation and tests is made up. Domains are reserved ones
that can never be registered (`.example`, `.test`, `example.com`), so nothing here points at a real organisation.

## License

[MIT](LICENSE)
