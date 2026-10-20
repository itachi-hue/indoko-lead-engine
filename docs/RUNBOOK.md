# Runbook

How to run the pilot day to day. The design is in [DESIGN.md](DESIGN.md).

Every command records a row in the `runs` table. Commands that write to the Sheet or Instantly accept `--dry-run`, which shows what would happen without doing it.

## One-time setup (M0)

1. `uv sync`, then copy `config.example.toml` to `config.toml` and `.env.example` to `.env`, and fill both in.
2. Create a Google service account. Save its key **outside the repo** and run `chmod 600` on it. Put the path in `GOOGLE_SERVICE_ACCOUNT_FILE`.
3. Have an Indoko user create a Google Sheet and share it with the service account's email as an editor. Put its ID in `sheet.spreadsheet_id`.
4. Share each warm-list Excel file with the service account. Add a `[[drive.warm_files]]` block for each one, and set `consent_confirmed = true` only after Karishma confirms those people agreed to hear from Indoko.
5. Create one Instantly campaign per group:
   - The three steps are `{{subject}}` / `{{body_html}}`, then `{{fu1_html}}`, then `{{fu2_html}}`.
   - Turn on Instantly's unsubscribe link.
   - Put the campaign IDs in `[instantly.campaigns]`.
6. Pick an email verifier and set `emails.verifier` and `VERIFIER_API_KEY`.
7. Run these three commands:

   ```bash
   uv run lead-engine init --sheet
   uv run lead-engine check --live
   uv run lead-engine check
   ```

   `check --live` prints the available Gemini models (set `gemini.model`) and whether the Instantly key has API access. Without API access, set `instantly.mode = "csv"`.
8. **Verify these Instantly details** (docs/DESIGN.md section 16) and adjust `src/lead_engine/instantly/client.py` and `events.py` if they differ:
   - lead status codes
   - interest status codes
   - the custom-variable format
   - the skip-if-in-workspace flag
   - whether the unsubscribe link adds a List-Unsubscribe header
9. Optional: `scripts/install-launchd.sh` adds a daily run at 08:00, and a backup plus retention run at 02:30.

## Warm list (M1)

```bash
uv run lead-engine import-warm
uv run lead-engine draft --group warm --limit 600
uv run lead-engine review push
# Karishma reviews the Review tab: Approve / Edit / Reject
uv run lead-engine run-daily   # events -> review pull -> send -> CRM
```

## Cold groups (M2–M4)

```bash
uv run lead-engine source --group engineer --limit 100
uv run lead-engine source --group founder --limit 100
uv run lead-engine source --group researcher --limit 100 --field computer-science
uv run lead-engine source --group creator --limit 100
uv run lead-engine score
uv run lead-engine find-emails --group founder --budget 5
uv run lead-engine draft --group founder
uv run lead-engine review push
uv run lead-engine linkedin   # engineers with no personal email -> LinkedIn tab
```

## Every day

- **`run-daily`** runs automatically, or by hand. It refuses to send if events haven't been pulled in 26 hours, if sending is paused, or if a daily, contact or monthly limit has been reached.
- **The team reviews new rows** in the Review tab and records booked, attended, one-pager sent and client in the CRM tab. The one-pager agent can write those columns too.

## Every week

- **Run `lead-engine report`.** It writes funnel numbers to the Metrics tab, with 95% ranges.
- **Check Google Postmaster Tools** and enter the complaint rate in the Metrics tab by hand:
  - 0.1% or more: stop raising volume.
  - 0.3% or more: pause sending in Instantly.
- **Tag the reason for each reply** in the CRM tab's Reply reason column.

## CSV mode (no Instantly API)

1. `lead-engine send` writes `data/exports/instantly_<campaign>_<time>.csv`. Upload it to that campaign.
2. After each send day, export the campaign's lead list from Instantly, then run:

   ```bash
   uv run lead-engine events --from-csv <file>
   ```

3. Add the addresses in `data/exports/suppression-export.csv` to Instantly's blocklist.
4. Delete the leads listed in `data/exports/remove-from-instantly-<date>.csv` from Instantly.

## When something goes wrong

| Situation | What to do |
|---|---|
| "sending is paused: 7-day bounce rate ..." | Find the source of bad addresses (`enrichment_ledger`, by provider). Fix it, then run `lead-engine resume` and resume the campaigns in Instantly |
| "`events` last succeeded ...h ago" | Run `lead-engine events` (or `events --from-csv`) and confirm it succeeds |
| A draft keeps failing checks | It shows up as `needs_manual`. Write it by hand in the Edited fields and set Edit |
| Someone asks to be removed | `lead-engine suppress <email>`. If they also want their data deleted, run `lead-engine forget <email>` |
| A domain must never be contacted | `lead-engine suppress @domain.com`, or add it to the Suppression tab |
| Two commands at once | The second exits with "already running"; that's expected |

## Backups

- **`lead-engine backup`** encrypts the database with `age` to `backup.age_recipient`, uploads it to the Shared Drive folder, and deletes copies older than 14 days.
- **It never uploads an unencrypted file.**
- **The `age` private key** lives in the macOS Keychain and in Indoko's password manager, never on Drive.
- **To restore:**

  ```bash
  age -d -i key.txt backup.sqlite.age > data/lead_engine.sqlite
  ```

## Evaluation (M7, docs/DESIGN.md section 18)

**Labelling sheets** (written to `eval/data/`, which is git-ignored because they contain personal data):

```bash
uv run lead-engine eval sample sourcing      # 25 per group
uv run lead-engine eval sample dedupe        # 50 people
uv run lead-engine eval sample evidence      # 50 facts
uv run lead-engine eval sample scoring       # 30 pairs
uv run lead-engine eval sample emails        # 30 found addresses (+ find rate and cost per group)
uv run lead-engine eval sample drafts        # 50 drafts, for Karishma to score 1-5 and approve/edit
# fill in the label_ columns, then:
uv run lead-engine eval score <stage> eval/data/<file>.csv
```

**Automatic checks:**

```bash
uv run lead-engine eval checks       # the six checks against the 40 labelled drafts
uv run lead-engine eval invariants   # pass-or-fail rules over every send; `send` runs these too
uv run lead-engine eval pilot        # funnel per group, 95% ranges, scale / fix / stop decision
```

**About the results:**

- Every result is appended to `eval/data/results.jsonl`.
- Run `eval pilot` at pilot week 4, and again at week 8 or later, because clients close late.
- Agree the decision rules with Karishma before the pilot starts.
