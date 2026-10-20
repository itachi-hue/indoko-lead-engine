# Indoko Lead Engine: Design

Design doc · Vivek · v3 · October 20, 2026

How Indoko's client-acquisition system gets built. It's a Python pipeline that:
- finds senior engineers, founders, researchers and creators;
- finds a usable email for each person;
- drafts a personal email with Gemini;
- waits for approval in a Google Sheet;
- sends through Instantly;
- records every outcome in Indoko's CRM sheet.

---

## 1. Goals and scope

**v1 delivers:**

- A warm-list campaign to the existing ~1,400 contacts (400 platform signups and the Excel list on Drive).
- Cold sourcing for four groups: senior engineers at FAANG and similar companies; founders; researchers; and artists, media people and creators. 100 people per group for the pilot.
- A personal email for every person, drafted from public evidence about them and approved by a person before sending.
- Sending through Indoko's Instantly account: by API when the plan allows it, by CSV otherwise.
- Every outcome recorded (emailed, replied, booked, attended, one-pager sent, client), plus one suppression list shared by every step.
- A weekly metrics tab, split by group and by warm or cold:
  - delivered, bounces and complaints
  - positive replies
  - booked and attended
  - clients and cost per client

**Recipients:** people located in the US only. Contacts in the EU, UK and Canada are skipped in v1, because their email laws (GDPR, CASL) need a different process.

**Pilot volume.** The first wave is 400 cold contacts plus 600 warm ones. That fits the Instantly Growth plan's 1,000-contact limit. The other ~800 warm contacts go out once wave-1 sequences finish. Three emails each to 1,000 contacts is 3,000 emails a month, under Growth's 5,000-email monthly limit. Both limits are from Instantly's pricing page, checked October 20, 2026.

**Not in v1:**

- Automated LinkedIn actions of any kind
- Sending without approval
- Closing-stage nurture emails (case studies, events)
- Paid ads
- The move to hosting

LinkedIn connection requests to engineers are sent by hand from Karishma's account. The pipeline prepares the list and the note.

**Where it runs:** on Vivek's Mac for the pilot, scheduled with `launchd`. It moves to Vercel or similar after the pilot (section 12).

---

## 2. Architecture

One Python package, with a command for each stage. A local SQLite database is the system of record. A Google Sheet is the shared workspace for Indoko's team, holding the review queue, the CRM and the metrics. Section 5 defines exactly which fields each one owns.

```
SOURCES     Warm list (Excel on Drive) · Exa search (engineers, founders, creators)
            OpenAlex / Semantic Scholar / arXiv (researchers) · GitHub (evidence only, no emails)
               │
               ▼
CORE        Ingest + identity ──► Score ──► Find email ──► Draft (Gemini + checks)
            (normalize, dedupe)   (achievements)  (by group policy,   │
                                                  verify)             │
               ┌──────────────────────────────────────────────────────┘
               ▼
REVIEW      Google Sheet · Review tab (approve / edit / reject)
               │ approved rows only
               ▼
SEND        Instantly (API push, or CSV upload) ──► Events (polled: replies, bounces, unsubscribes)
               │                                        │
               ▼                                        ▼
REPORT      Google Sheet · CRM tab ◄──► SQLite (field ownership in section 5) · Metrics tab weekly
```

**Stack:**

- Python 3.12, managed with `uv`
- SQLite now, Postgres once hosted
- `httpx` for API calls, with one shared retry policy (section 12)
- `openpyxl` to read the Excel files
- The Google Drive and Sheets APIs
- The Gemini API, paid tier
- Instantly API v2

**Google files.** A service account has no storage of its own on Drive, so it can't own files or upload backups. So:

- **The spreadsheet** is a native Google Sheet owned by an Indoko user and shared with the service account as an editor.
- **The Excel files** stay as they are. The importer downloads them through the Drive API and reads them with `openpyxl`. Nothing is written back to them.
- **Backups** go to a Shared Drive folder, which the service account can write to.

**Commands.** Each one can be re-run safely; it skips work already done.

```
lead-engine import-warm             # download Excel files from Drive, normalize, de-duplicate
lead-engine source --group engineer --limit 100
lead-engine score
lead-engine find-emails --budget 10  # dollars; stops when the cap is reached
lead-engine draft --group engineer
lead-engine review push|pull         # sync drafts to and from the Review tab
lead-engine send                     # approved drafts to Instantly (API or CSV)
lead-engine events                   # poll replies, bounces, unsubscribes
lead-engine crm push|pull            # pipeline fields out, CRM-owned fields in
lead-engine report                   # weekly metrics tab
lead-engine forget <email>           # delete a person; keep a hashed suppression entry
lead-engine run-daily                # events → review pull → send → crm pull/push; supports --dry-run
```

---

## 3. Segments

Each group has its own sources, scoring signals, email policy and angle. Emails mention visas only as a general list (O-1, EB-1A, EB-2 NIW), never matched to the person.

| Group | Sources | Signals scored | Email policy | Email angle |
|---|---|---|---|---|
| Senior engineers | Exa people search ("staff or principal engineer at Google, US"), GitHub for evidence, Exa for patents and talks | Patents, papers, conference talks, notable open-source work, press, seniority | **Personal address published by the person only** (section 6). Without one, the person goes on the manual LinkedIn list | Their patent, talk or project, and long-term options in the US |
| Founders | Exa search over funding news and the YC directory | Recent round and its size, press coverage, investors, prior exits | A work email at their own company is fine | Their recent round or launch |
| Researchers | OpenAlex, Semantic Scholar, arXiv | Citations, h-index, recent papers, venue, US institution | Address published on their faculty or lab page | A specific recent paper |
| Creators and media | Exa search for press features and awards | Major-outlet coverage, awards, audience size, festival or exhibition history | Business contact published on their own site. Skip if only a manager is listed | A feature, award or recent work |
| Warm list | Excel files on Drive, platform signups | None needed; group assigned from known fields where possible | Email already on file | A consultation offer from someone they've already heard from |

**Scoring inputs are achievements only.** Name, nationality, ethnicity, photos, guessed immigration status, and employer sponsorship history are never collected or scored.

**Group names in code** are singular: `engineer`, `founder`, `researcher`, `creator`, `warm`. This matches the prototype's `persons.segment` column.

**US location.** `us_located` comes from the source record: the location on the Exa profile, the institution's country in OpenAlex or ROR, or the warm-list field. If the location is unknown, it counts as not in the US, and the person is skipped.

**One person, one list.** Cold sourcing skips anyone who is already on the warm list or has already been contacted. Each person has at most one active draft.

---

## 4. Data model

SQLite. The first six tables already exist in the prototype (`o1-leadgen/db.py`) under these names. Four are new.

| Table | Holds | Change |
|---|---|---|
| `persons` | Name, employer, `segment`, country | Add `group_source` (warm or cold) and `us_located` |
| `person_identifiers` | Source IDs used to de-duplicate (OpenAlex, ORCID, GitHub, Exa URL, email) | None |
| `signals` | Facts about a person | Add `evidence_text`, `evidence_date` and `url`, so drafts can cite them and the grounding check can verify them. Remove the `public_visa_statement` type |
| `scores` | Versioned score and sub-scores | Sub-scores change (section 11) |
| `enrichment_ledger` | Every email lookup: provider, cost, result. Enforces the budget cap and prevents paying twice | None |
| `suppression` | Do-not-contact entries | Add `match_type` (`email`, `domain` or `email_hash`), `reason`, `source`, `added_at`. Matching covers exact emails, whole domains and hashed emails |

**New tables:**

```sql
CREATE TABLE drafts (
  draft_id        TEXT PRIMARY KEY,        -- ULID
  person_id       TEXT NOT NULL REFERENCES persons(person_id),
  batch_id        TEXT NOT NULL,
  segment         TEXT NOT NULL,           -- engineer|founder|researcher|creator|warm
  subject TEXT, body TEXT, fu1 TEXT, fu2 TEXT,
  evidence_ids    TEXT,                    -- JSON array of signals.signal_id
  model TEXT, prompt_version TEXT,
  checks          TEXT,                    -- JSON: check -> pass|fail + reason
  state           TEXT NOT NULL,           -- see section 5
  edited_subject TEXT, edited_body TEXT, edited_fu1 TEXT, edited_fu2 TEXT,
  reviewer TEXT, decided_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE sends (
  send_id           TEXT PRIMARY KEY,
  draft_id          TEXT NOT NULL UNIQUE REFERENCES drafts(draft_id),  -- idempotency key
  campaign_id       TEXT NOT NULL,
  instantly_lead_id TEXT,
  method            TEXT NOT NULL,         -- api|csv
  status            TEXT NOT NULL,         -- pending|sent|exported|failed
  attempted_at TEXT, sent_at TEXT, error TEXT
);

CREATE TABLE events (
  event_id     TEXT PRIMARY KEY,
  person_id    TEXT NOT NULL REFERENCES persons(person_id),
  draft_id     TEXT REFERENCES drafts(draft_id),
  type         TEXT NOT NULL,   -- sent|reply|positive_reply|bounce|unsubscribe|booked|attended|onepager_sent|client|not_interested
  source       TEXT NOT NULL,   -- instantly|crm|manual
  external_id  TEXT UNIQUE,     -- Instantly event or email ID; de-duplicates repeated polls
  occurred_at  TEXT NOT NULL,
  payload      TEXT             -- JSON, no email bodies
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, command TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT,
  ok INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, error TEXT
);
```

---

## 5. State and ownership

**Source of truth, by field:**

| Owner | Fields |
|---|---|
| SQLite | People, evidence, draft text, send and event state, the suppression list |
| Review tab | Decision, edited subject and body, reviewer, notes, until `review pull` reads them |
| CRM tab | Booked, attended, one-pager sent, client, owner notes |

- `crm push` writes only the fields SQLite owns, and never overwrites a field the CRM tab owns.
- `crm pull` reads the CRM-owned fields and records them as `events` with `source = crm`.

**Row identity.** Every Sheet row has a hidden `draft_id` or `person_id` column. Syncing matches rows on that ID, never on row number, so the team can sort and filter freely.

**Draft states:**

```
drafted → pushed → approved | edited → queued → sent | exported → closed
                 ↘ rejected (final)
needs_manual → pushed with empty text → edited
stale (person suppressed after push; never sent)
```

**Decisions:**

- Only `approved` and `edited` drafts can move to `queued`. `rejected` is final.
- If Decision is Edit, or any edited field has text, the draft is `edited`. If Decision is Approve and all edited fields are empty, it's `approved`.
- `needs_manual` drafts (checks failed twice) are pushed with an empty subject and body. The reviewer writes the email in the edited fields and sets Edit. It then goes through the checks like any other edit.

**Pulling decisions:**

- `review pull` reads the whole tab once, as one snapshot, and re-checks each draft's state before applying a decision. It accepts decisions only for drafts in `pushed`.
- Edited text goes through the checks again. If it fails, the draft returns to `pushed`, with the reason in Notes.
- Once a draft is `queued`, its range is protected. Later edits are ignored and flagged in Notes.
- A row deleted from the Sheet leaves its draft in `pushed`, and the next `review push` adds it back.

**Stopping and closing:**

- If a person is suppressed after their draft was pushed, the draft becomes `stale` and is never sent.
- A draft becomes `closed` when its last step is sent, or on an unsubscribe, bounce or not-interested reply.

**Lead status** comes from the latest event, in this order of precedence:

```
client > attended > booked > positive_reply > reply > sent
```

`unsubscribe`, `bounce` and `not_interested` close the lead whatever came before. Status is computed from events, never stored on its own.

---

## 6. Finding emails

Providers are tried by group policy and in order of cost. Each lookup is recorded in `enrichment_ledger`, so nobody is looked up twice and spending stops at the cap.

| Group | Allowed sources, in order |
|---|---|
| Engineers | (1) An address the person published on their personal site, blog or talk page, found through Exa search and read from the page (about $1–8 per 1,000 people in Exa API credits). (2) Exa Websets enrichment, if Indoko's credits include Websets. **Hunter, Prospeo and Apollo are not used for engineers**, because they mostly return work addresses |
| Founders | Published address → Exa Websets → Hunter, Prospeo or Apollo free tiers → Icypeas (~$19/month, only if needed) |
| Researchers | Address published on their faculty or lab page → OpenAlex or ORCID links → free tiers |
| Creators | Business contact published on their own site. Manager-only contacts are skipped |

**Verification.** Every address is verified before it can be drafted. The verifier sits behind an interface and is set in config. The default is a low-cost hosted verifier with pay-per-check credits, chosen in M0. A self-hosted verifier such as Reacher only works on a host with outbound port 25 open. Home connections and most cloud hosts block that port, so it won't work from the Mac.

| Result | Accepted? |
|---|---|
| `deliverable` | Yes |
| `catch_all` | Only if the person published the address themselves |
| `unknown` or anything else | No |

Role addresses (info@, hello@) and disposable domains are always dropped.

**Hard rules:**

- No emails taken from GitHub profiles or commit history.
- No guessed addresses.
- For engineers, any address on their employer's domain, or on a known corporate domain list, is rejected.

---

## 7. Writing emails

**What Gemini receives**, for each person:

- the person's first name and group;
- two or three evidence items, each with text, date and source URL;
- the group's template and tone rules;
- the sender name.

Email addresses are never sent to Gemini.

**What Gemini returns** is JSON with the subject, the body, two follow-ups and the IDs of the evidence it used. The model and prompt version are set in config and stored on each draft.

**Checks.** Every draft passes these checks before it reaches the Review tab. If a draft fails, it is regenerated once. A second failure marks it `needs_manual`, for a person to write. Invalid JSON or a safety block from Gemini counts as a failure.

| Check | Fails when |
|---|---|
| Grounded | Mechanical: a capitalized multi-word phrase or a number in the subject or body doesn't match the cited `evidence_text` or the allowlist. Numbers are compared after normalizing ("1.2M" = 1,200,000). The allowlist covers the recipient's name, the sender, Indoko, the visa names and the booking link. Claims without names or numbers are left to the reviewer |
| No eligibility claims | It uses words like "qualify", "eligible", "guarantee" or "approved", or matches a specific visa to the person |
| No sensitive traits | It refers to nationality, ethnicity, country of origin or current visa status |
| Length and form | The body is over 120 words, the subject is over 8 words, or there is more than one link or more than one ask |
| Footer | The template's footer block is missing or altered |
| Tone | It contains stock flattery or filler from a banned-phrase list |

Each check is measured against a fixed set of 40 hand-labelled drafts (acceptance criteria in section 14).

---

## 8. Review and sending

**Review tab columns:**

| Column | Notes |
|---|---|
| `draft_id`, `person_id` | Hidden |
| Batch, group, name, title and company | |
| Evidence links | |
| Subject, body, follow-up 1, follow-up 2 | |
| Check notes | |
| **Decision** | Dropdown: Approve, Edit, Reject |
| Edited subject, edited body, edited follow-up 1, edited follow-up 2 | |
| Reviewer, notes | |
| State | Read-only |

**Flow:**

1. `review push` adds new drafts, grouped by batch.
2. The reviewer sets a decision on each row.
3. `review pull` applies the decisions, following the state rules in section 5.
4. `send` takes each approved draft and:
   - re-checks suppression in the same database transaction that marks it `queued`;
   - writes a `sends` row with `status = pending` and commits;
   - adds the lead to that group's Instantly campaign (`POST /api/v2/leads`, skipping leads already in the workspace);
   - records the lead ID and sets `status = sent`.
5. **Recovery after a crash.** `send` looks up every `pending` row by email in its campaign before retrying, so nobody is added twice.
6. **Without API access,** `send` writes a CSV in Instantly's import format, sets `status = exported`, and someone uploads it.
7. `events` polls Instantly for:
   - sent emails;
   - replies, with positive replies taken from the lead's interest status set in Instantly's inbox;
   - bounces;
   - unsubscribes.

   It writes them to `events`. Unsubscribes and hard bounces go to `suppression` and to Instantly's blocklist on the same run.

**Instantly field mapping.** Each lead's custom variables, all plain strings:

| Variable | Contents |
|---|---|
| `subject` | Subject line |
| `body_html` | First email; newlines converted to `<br>` |
| `fu1_html`, `fu2_html` | The two follow-ups, same conversion |
| `first_name` | |
| `draft_id` | Links Instantly back to the draft |

Each campaign's three steps are just `{{subject}}` and `{{body_html}}`, then `{{fu1_html}}`, then `{{fu2_html}}`.

**The 1,000-contact limit.** Leads whose sequence has finished are archived to SQLite (events, status and history) and then removed from Instantly. Unsubscribes and bounces stay on Instantly's blocklist. Wave 2 starts only after enough wave-1 leads have been removed. If wave 2 has to start sooner, we upgrade to Hypergrowth ($97/month).

**Positive replies** come from Instantly. **Booked, attended and client** come only from the CRM tab.

**Duplicates.** If Instantly skips a lead because it's already in the workspace, the send is marked `failed` with `error = duplicate`, never `sent`.

**Unsubscribe replies.** `events` reads the first 200 characters of each reply. If they contain "unsubscribe", "remove" or "stop" (any capitalization), the reply is recorded as `unsubscribe`. Ambiguous replies are flagged in the CRM tab for a person to check.

**CSV mode.** Without API access, the steps that read from Instantly become manual:

- After each send day, someone exports the campaign's lead list from Instantly.
- `lead-engine events --from-csv <file>` imports status, replies, bounces and unsubscribes from that file.
- The bounce auto-pause becomes a daily manual check against the same file.
- Unsubscribes are added to Instantly's blocklist by hand, from a `suppression-export.csv` the command writes.

**CRM tab columns:**

| Owned by the pipeline | Owned by the team (and the one-pager agent) |
|---|---|
| `person_id` (hidden), name, group, warm or cold, email, status (computed, read-only), first sent, last event, positive reply date | Booked, attended, one-pager sent, client, owner, notes |

**Suppression tab columns:** email or domain, reason, added by, date. `crm pull` imports new rows with `source = manual`.

**Metrics tab:**

- Complaint rates come from the weekly Postmaster Tools check and are entered by hand.
- **Cost per client** is total spend divided by clients. Total spend is Exa, email finders, the verifier and Gemini (all recorded in `runs`), plus monthly tool fees prorated.

---

## 9. Compliance and deliverability

**Every email includes:**

- accurate sender details;
- an honest subject line;
- the Indoko postal address;
- a line saying it's a message about Indoko's services (CAN-SPAM requires commercial email to identify itself);
- Instantly's unsubscribe link, with a List-Unsubscribe header.

Replying "unsubscribe" is honored too.

**Opt-out handling:**

- Opt-outs take effect within 24 hours, well inside CAN-SPAM's 10-business-day limit.
- They go to `suppression` and to Instantly's blocklist.
- Nobody who opted out is ever re-imported, including from future warm-list exports.

**Sending limits per inbox (new contacts a day):**

- 15 in week 1, then 10 more each week, up to 40.
- The number of inboxes multiplied by the daily cap must cover the planned volume. Section 17 asks how many inboxes Indoko has.

**Bounces.** Once the last 7 days include at least 50 sends, a hard-bounce rate above 2% makes `events` pause the campaign through the API and alert Vivek. In CSV mode, this is a daily manual check.

**Complaints.** Instantly doesn't receive spam-complaint data from Gmail, so complaints are checked weekly by hand in Google Postmaster Tools:

| Complaint rate | Action |
|---|---|
| 0.1% or more | Stop raising volume |
| 0.3% or more | Pause sending |

**Engineers** are never emailed at their employer's address.

---

## 10. Privacy and security

- **Gemini:** paid API tier only, because the free tier may use inputs for training. Briefs contain names, groups and evidence, never email addresses.
- **Database:** the SQLite file lives on the Mac's FileVault-encrypted disk.
- **Backups:** encrypted with `age` before upload to a Shared Drive folder, and kept for 14 days. The `age` private key is stored in the macOS Keychain and in Indoko's password manager, never on Drive.
- **Service-account key:** stored outside the repo, with `chmod 600`.
  - Its access is limited to `spreadsheets` and `drive.file` scopes.
  - Only the specific files it needs are shared with it.
- **The spreadsheet** is shared with named Indoko users only. No link sharing.
- **Logs** contain IDs and counts, never email addresses or email bodies.
- **Retention:** people who were never contacted are deleted after 90 days.
- **Deletion requests:** `lead-engine forget <email>` does three things:
  1. Deletes the person's rows in `persons`, `person_identifiers`, `signals`, `drafts`, `sends` and `events`.
  2. Removes the person from the Sheet tabs.
  3. Stores `sha256(lowercase(email))` in `suppression` with `match_type = email_hash`, so they're never contacted again. Suppression checks hash each candidate address before comparing.
  4. Deletes the lead from every Instantly campaign and adds the address to Instantly's blocklist. In CSV mode, it writes the address to `suppression-export.csv` instead.
- **Prototype data** (`.env`, `raw_cache/`, CSVs, databases) is never copied into this repo. Only code moves over.

---

## 11. Reusing existing code

The earlier O-1 sourcing prototype (`~/o1-leadgen`, about 2,350 lines of Python) is the starting point. Only code moves over, never data.

| Module | Plan |
|---|---|
| `db.py` | Keep the existing table names. Add the columns and tables in section 4, plus domain matching in `is_suppressed` |
| `identity.py` | Keep. It matches people across sources without risky auto-merges |
| `openalex_source.py`, `semanticscholar.py`, `arxiv_source.py` | Keep, for researcher sourcing and evidence |
| `github_engineers.py` | Keep for evidence. **Stop storing email identifiers** |
| `engine.py`, `scoring.py` | Keep the structure. **Remove** the `immigration_relevance`, `high_remuneration` and `employer_distinction` sub-scores, and with them the imports of `dol` and `employers`. Re-weight what remains. Add founder and creator signals |
| `enrich_db.py` | Keep the ledger and budget cap. **Delete** the GitHub public-email channel and the employer-domain lookups (`DOMAINS`). Add the per-group policy and verification from section 6 |
| `enrich.py` | Keep only `scrape_site_email`, which reads published addresses from personal pages |
| `net.py` | Replace with an `httpx` client using the retry policy in section 12 |
| `email_creative.py` | Replace with the Gemini drafter and checks |
| `dol.py`, `employers.py`, `github_corr.py`, `juicebox_*` | Not used |
| New | Exa source, warm-list importer, Sheets sync, Instantly client, scheduler, `forget` |

---

## 12. Running it

**Pilot.** It runs on Vivek's Mac:

- A `launchd` job runs `run-daily` each morning: pull events, pull review decisions, send, sync the CRM. Events come first, so overnight unsubscribes and bounces apply before anyone new is contacted.
- Sourcing, scoring and drafting run on demand, one batch at a time.
- Secrets live in `.env`, which is never committed. The repo includes a `.env.example`.

**Failure handling:**

- **Retries:** HTTP 429 and 5xx errors are retried up to 4 times with exponential backoff, honoring `Retry-After`. Other 4xx errors fail that one item, not the whole run.
- **Partial runs:** each item commits on its own. The `runs` row records how many items succeeded, failed or were skipped.
- **One run at a time:** every command takes a file lock (`~/.lead-engine/run.lock`). A second command started while one is running exits with "already running."
- **No sending blind:** `run-daily` refuses to send if `events` hasn't succeeded in the last 26 hours. Unsubscribes and bounces must be current before anyone new is contacted.
- **Sheets limits:** writes are batched with `batchUpdate` and kept under 50 requests a minute.
- **Alerts:** an error, the budget cap, or the bounce limit sends Vivek an email from an internal inbox, never from the sending domains.

**After the pilot.** It moves to hosting.

- On Vercel, scheduled runs use Vercel Cron and the database moves to hosted Postgres.
- Long jobs are split into small batches, because serverless functions have run-time limits.
- If batching gets awkward, an always-on host (Railway, Render or Cloud Run) suits this pipeline better.
- Storage, scheduling and the lock sit behind small interfaces, so moving is a configuration change, not a rewrite.

---

## 13. Configuration

```toml
# config.toml (secrets stay in .env)
[pilot]
us_only = true
groups = ["engineer", "founder", "researcher", "creator"]
per_group_limit = 100

[gemini]
model = "<set in M0>"
prompt_version = "v1"
max_regenerations = 1

[emails]
verifier = "<hosted verifier, chosen in M0>"
budget_usd_per_run = 10
engineer_policy = "published_personal_only"

[instantly]
mode = "auto"                      # api if the key works, else csv
campaigns = { engineer = "", founder = "", researcher = "", creator = "", warm = "" }
contact_limit = 1000

[sending]
daily_new_per_inbox = [15, 25, 35, 40]   # by week
bounce_pause_pct = 2.0

[sheet]
spreadsheet_id = ""
review_tab = "Review"
crm_tab = "CRM"
suppression_tab = "Suppression"
metrics_tab = "Metrics"

[retention]
uncontacted_days = 90
backup_days = 14
```

---

## 14. Build plan

The dates assume about half of Vivek's time.

| Milestone | Dates | Contents | Done when |
|---|---|---|---|
| M0 · Setup | Oct 21–23 | Seed code from `o1-leadgen` (code only); `uv` project; config; service account; the Sheet; check API keys; verify the Instantly items in section 16 | `lead-engine --help` runs; every key checks out; Instantly API access and limits confirmed |
| M1 · Warm list | Oct 26–Nov 6 | Drive importer, cleaning, de-duplication, suppression, grouping, drafts, Review tab, send, events, CRM sync | Wave-1 warm batch approved and sent; replies appear in the CRM tab; the end-to-end test in section 15 passes |
| M2 · Cold sourcing | Oct 28–Nov 13 | Exa source for engineers, founders and creators; researcher sources wired in; revised scoring | 100 scored, US-located people per group, each with at least 2 dated evidence items |
| M3 · Emails | Nov 4–Nov 13 | Group policies, published-page finder, free-tier finders, verifier, budget cap | Find rate and cost per verified address reported per group |
| M4 · Writing | Nov 9–Nov 20 | Gemini drafter, checks, follow-ups, LinkedIn list for engineers | Each check catches every banned item in the 40-draft test set, with at most 10% false alarms; Karishma approves at least 70% of 50 sample drafts without edits |
| M5 · Pilot | Nov 23–Dec 18 | Staged sending to 400 cold contacts plus the warm waves; daily runs; weekly metrics | Under 3% bounces on the first 100 sends; four weeks of metrics by group |
| M6 · Hosting | January 2027 | Postgres, scheduled runs on the chosen host | Runs without Vivek's laptop |

M1 ships first, so the warm campaign starts while the cold pipeline is being built.

---

## 15. Testing

- **Unit tests** cover:
  - suppression (email and domain);
  - employer-domain rejection for engineers;
  - de-duplication;
  - every draft check, against the 40-draft labelled set;
  - the budget cap;
  - the draft state rules;
  - send idempotency, including recovery of `pending` rows;
  - a Sheet edit made while `review pull` is running, which must not be applied against a stale state;
  - `events --from-csv` imports of every status, using a sample Instantly export.
- **Recorded API responses** for Exa, Gemini, Instantly and Sheets let the tests run offline and for free.
- **`--dry-run`** works on every command that writes outside the database. It shows what would happen and does nothing.
- **End-to-end test before the first real send,** using internal addresses:
  1. draft, push, approve and send;
  2. a reply, a positive reply, an unsubscribe and a bounce;
  3. confirm each one lands correctly in `events`, the CRM tab, `suppression` and Instantly's blocklist.

---

## 16. Risks and open questions

**To verify in M0.** These come from third-party sources or are unconfirmed; check them in the Instantly account before building on them:

- whether Growth includes API v2 access, and whether webhooks need a higher plan (the design polls, so it doesn't depend on webhooks);
- how custom variables are formatted;
- the parameter for skipping leads already in the workspace;
- where the reply interest status is exposed;
- whether Instantly's unsubscribe link adds a List-Unsubscribe header;
- whether removing a lead frees contact-limit capacity right away.

| Item | Why it matters | Plan |
|---|---|---|
| Exa credit type | Websets credits are separate from API credits | Ask Karishma. The published-page finder works with API credits alone |
| Instantly plan | Growth: 1,000 contacts and 5,000 emails a month; API access unconfirmed | Waves, archiving, CSV fallback; upgrade only if needed |
| Engineers' personal emails | Likely the lowest find rate of any group | Measure in M3; the LinkedIn list covers the rest |
| Warm-list origin | People must have agreed to hear from Indoko | Confirm before M1 sends; import only confirmed sources |
| Inbox capacity | Daily caps × inboxes limit volume | Ask how many inboxes Indoko has; plan the waves to fit |
| Draft quality | Generic or wrong emails hurt replies and reputation | Grounding checks, plus human review of every email in the pilot |
| Laptop as host | Runs stop when the Mac sleeps | The daily schedule catches up on wake; hosting comes in M6 |

---

## 17. What I need to start

- API keys for Exa, Gemini (paid tier) and Instantly, plus the Instantly plan name and how many warmed-up inboxes there are.
- The Excel files on Drive, shared with the service account. A Google Sheet owned by Indoko, for the Review, CRM, Suppression and Metrics tabs. A Shared Drive folder for backups.
- The sender name, the Indoko postal address for the footer, and the booking link.
- Who reviews drafts in the Sheet, and how often.
- Where the warm list came from, so only confirmed contacts are imported.
