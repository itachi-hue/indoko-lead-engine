# Indoko Lead Engine

A pipeline that finds people with strong professional records (senior engineers, founders, researchers, creators), finds a usable email for each, and drafts a personal email with Gemini. Every draft is approved in a Google Sheet before Instantly sends it. Outcomes are tracked in Indoko's CRM sheet.

The design is in [docs/DESIGN.md](docs/DESIGN.md).

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp config.example.toml config.toml   # fill in IDs, sender details, booking links
cp .env.example .env                 # fill in API keys; never commit .env
uv run lead-engine init              # create the local database
uv run lead-engine check             # config and keys present?
uv run lead-engine check --live      # call each API once (Exa costs ~$0.007)
uv run lead-engine init --sheet      # create the Sheet tabs and headers
```

The Google service account key file must live outside the repo, with `chmod 600`. Share only the specific spreadsheet and Drive files it needs with the service account's email.

## Tests

```bash
uv run pytest
```

Tests use recorded and faked API responses, so they run offline and cost nothing.
