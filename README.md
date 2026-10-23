# Indoko Lead Engine

A pipeline for Indoko's outreach. It does four things:

1. Finds **senior engineers, funded founders and researchers who moved to the US recently**, the
   people most likely to be weighing an O-1, EB-1A or EB-2 NIW.
2. Finds and checks an email address for each person.
3. Drafts a personal email with Gemini.
4. Puts every draft up for approval in a Google Sheet. Approved drafts are sent with Instantly, and
   outcomes are tracked in the CRM tab.

**Docs:**

- **Who we target and why, with costs and live results:** [docs/ICP_SOURCING_PLAN.md](docs/ICP_SOURCING_PLAN.md)
- **How the pipeline runs:** [docs/DESIGN.md](docs/DESIGN.md)
- **Operating it:** [docs/RUNBOOK.md](docs/RUNBOOK.md)
- **Hosting:** [docs/HOSTING.md](docs/HOSTING.md)

## How sourcing works

| Group | Where people come from | How we tell they moved to the US | Cost each |
|---|---|---|---|
| Engineers | Exa people search at 13 companies whose mail servers confirm mailboxes (Microsoft, Apple, NVIDIA, Intel, Tesla, LinkedIn, …) | First school or first job outside the US, first US year within 15 years | ~$0.01 |
| Founders | Exa people search for current US founders, plus a news headline confirming a raise in the last 18 months | Same | ~$0.05–0.09 |
| Researchers | OpenAlex | Career started abroad, first US institution within 10 years; government institutions skipped | Free |

**Rules the pipeline follows:**

- **No guessing from identity.** The move is read only from where someone studied, worked or filed
  patents, never from names, ethnicity or photos.
- **Never mentioned in an email.** The move is stored for targeting only. Drafts are checked so they
  never mention a country, where someone came from, or their immigration status.
- **Addresses must be confirmed.** Guessed addresses are used only if the verifier confirms the
  mailbox. Servers that accept every address are skipped.
- **Recipients and approval.** Recipients must be in the US, and every draft is approved by a person
  before it is sent.

## Status

Sourcing has run live: 90 people who moved to the US recently, about $5 of Exa credits in total.

**Built and tested offline (249 tests):**

- email checks (parallel);
- drafting;
- the Review and CRM tabs;
- sending;
- event tracking;
- suppression;
- evaluation.

**Waiting on keys:**

| Key | Unlocks |
|---|---|
| Paid Reoon plan (`VERIFIER_API_KEY`) | Confirming emails at volume |
| `OPENALEX_API_KEY` (free) | Researchers without rate-limit blocks |
| `GEMINI_API_KEY` | Drafts |
| Google service account + `sheet.spreadsheet_id` | Review tab |
| `INSTANTLY_API_KEY` | Sending |

**Before the first real send:**

1. Run `lead-engine check --live`.
2. Verify the Instantly details listed in DESIGN.md section 16.
3. Run the end-to-end test with internal addresses (DESIGN.md section 15).

**Parked:** the warm list and creators. Hosting (Vercel) is prepared but not deployed.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp config.example.toml config.toml   # sender details, booking link, Sheet ID
cp .env.example .env                 # API keys; never commit .env
uv run lead-engine init --sheet      # local database + Sheet tabs
uv run lead-engine check --live      # every key and API, once (Exa costs ~$0.007)
```

The Google service-account key lives outside the repo, with `chmod 600`. Share only the specific
spreadsheet and Drive files with the service account's email.

## Running it

```bash
# everything, for each group in pilot.groups; steps without keys are skipped with a note
caffeinate -i uv run lead-engine pipeline --limit 50

# then, every day: events -> review pull -> send -> CRM
uv run lead-engine run-daily
```

`caffeinate -i` keeps the Mac awake while a long run is going.

## Commands

| Stage | Commands |
|---|---|
| All at once | `pipeline [--groups engineer founder researcher] [--limit N] [--no-draft]` |
| Sourcing | `source --group engineer\|founder\|researcher [--via exa\|exa_people\|patents\|sec_form_d]`, `score` |
| Emails | `find-emails --group ... [--budget 5] [--discover-only]`, `check-domains` |
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
