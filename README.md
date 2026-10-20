# Indoko Lead Engine

A pipeline that finds people with strong professional records (senior engineers, founders, researchers, creators), finds a usable email for each, and drafts a personal email with Gemini. Every draft is approved in a Google Sheet before Instantly sends it. Outcomes are tracked in Indoko's CRM sheet.

- **Design:** [docs/DESIGN.md](docs/DESIGN.md)
- **Operating it:** [docs/RUNBOOK.md](docs/RUNBOOK.md)
- **Hosting:** [docs/HOSTING.md](docs/HOSTING.md)

## Status

Milestones M0–M7 are built and tested offline. Before the first real send:

1. Fill in config and keys, then run `lead-engine check --live`.
2. Verify the Instantly details listed in DESIGN.md section 16.
3. Run the end-to-end test with internal addresses (DESIGN.md section 15).

M6 hosting is prepared but not deployed.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp config.example.toml config.toml   # fill in IDs, sender details, booking links
cp .env.example .env                 # API keys; never commit .env
uv run lead-engine init --sheet      # local database + Sheet tabs
uv run lead-engine check --live      # every key and API, once (Exa costs ~$0.007)
```

The Google service-account key lives outside the repo, with `chmod 600`. Share only the specific spreadsheet and Drive files with the service account's email.

## Commands

| Stage | Commands |
|---|---|
| Warm list | `import-warm` |
| Cold sourcing | `source --group engineer\|founder\|researcher\|creator`, `score` |
| Emails | `find-emails --group ... --budget 5` |
| Writing | `draft --group ...`, `linkedin` |
| Review | `review push`, `review pull` |
| Sending | `send`, `events` (or `events --from-csv`), `run-daily`, `resume` |
| CRM | `crm push`, `crm pull` |
| People | `suppress <email or @domain>`, `forget <email>` |
| Operations | `report`, `backup`, `retention` |
| Evaluation | `eval sample\|score\|checks\|invariants\|pilot` |

Every command accepts `--dry-run`. Only one command runs at a time.

## Tests

```bash
uv run pytest
```

Tests use faked and recorded API responses, so they run offline and cost nothing.
