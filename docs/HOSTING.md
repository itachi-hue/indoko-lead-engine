# Hosting (M6)

The pilot runs on Vivek's Mac (see [RUNBOOK.md](RUNBOOK.md)). This page covers moving the scheduled
part to a host so it runs without the laptop. Nothing here has been deployed yet.

## What moves, and what stays manual

| Runs on the host on a schedule | Stays manual, run from a laptop against the same database |
|---|---|
| `run-daily` (events → review pull → send → CRM) | `source`, `find-emails`, `draft`, `review push`, `linkedin` |
| `report` (weekly) | `forget`, `suppress`, `resume` |
| `retention` (daily) | |

Sourcing and drafting take minutes and call paid APIs in loops, so they stay out of serverless time limits.

## Database

Set `[storage] backend = "postgres"` and `DATABASE_URL`, then install the extra: `uv sync --extra postgres`.

- **Providers:** any hosted Postgres works (Neon, Supabase, Vercel Postgres, Railway).
- **Locking:** the single-run lock becomes a Postgres advisory lock, so two invocations can never send at the same time.
- **Backups:** use the provider's backups. `lead-engine backup` is for the SQLite pilot only.
- **Migration:** export the pilot's SQLite tables and import them into Postgres with the same schema, by hand, once.

**Verify on first deploy:**

1. Run `lead-engine init` and `lead-engine check --live` against Postgres.
2. Run the end-to-end test (DESIGN.md section 15) with internal addresses.

The Postgres adapter (`src/lead_engine/storage.py`) is unit-tested, but it has not yet run against a live Postgres server.

## Configuration without files

On a host there's no `config.toml` or key file, so set these environment variables instead:

| Variable | Contents |
|---|---|
| `LEAD_ENGINE_CONFIG_TOML` | the whole `config.toml` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the service-account key JSON |
| `EXA_API_KEY`, `GEMINI_API_KEY`, `INSTANTLY_API_KEY`, `VERIFIER_API_KEY` | API keys |
| `DATABASE_URL` | the Postgres connection string |
| `CRON_SECRET` | the shared secret for the cron endpoint |

On Vercel, also set `[paths] data_dir = "/tmp/lead-engine"` in the config. Its filesystem is read-only except `/tmp`.

## Option A: Vercel

- **Endpoint:** `api/cron.py` is the cron endpoint. It rejects any request without `Authorization: Bearer $CRON_SECRET` and only runs `run-daily`, `report`, `retention` and `events`.
- **Schedule:** `vercel.json` schedules `run-daily` at 15:00 UTC (08:00 Pacific in summer), the report on Mondays, and retention daily. Vercel cron times are UTC.
- **Time limit:** `maxDuration` is 300 seconds. A daily run of a few hundred sends fits; if it ever doesn't, run `send --limit` in smaller steps or move to option B.

## Option B: an always-on host (Railway, Render, Cloud Run jobs)

Better if runs get long. Use a scheduled job with the command `uv run lead-engine run-daily`, the same environment variables, and no time limit. This is the simplest choice if batching on Vercel gets awkward.
