# Indoko Lead Engine: Design

Design doc · Vivek · October 20, 2026 · v1

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
- Every status written to the CRM sheet (emailed, replied, booked, attended, one-pager sent, client), plus a shared suppression list.
- A weekly metrics tab, split by group and by warm or cold:
  - delivered, bounces and complaints
  - positive replies
  - booked and attended
  - clients and cost per client

**Not in v1:**

- Automated LinkedIn actions of any kind
- Sending without approval
- Closing-stage nurture emails (case studies, events)
- Paid ads
- The move to hosting

LinkedIn connection requests to engineers are sent by hand from Karishma's account. The pipeline prepares the list and the note.

**Where it runs:** on Vivek's Mac for the pilot, scheduled with `launchd`. It moves to Vercel or similar after the pilot (see [Running it](#11-running-it)).

---

## 2. Architecture

One Python package, with a command for each stage. Stages read and write a local SQLite database, which is the system's source of truth. Google Sheets is the shared view for Indoko's team: the review queue, the CRM and the metrics.

```
SOURCES     Warm list (Excel on Drive) · Exa search (engineers, founders, creators)
            OpenAlex / Semantic Scholar / arXiv (researchers) · GitHub (evidence only, no emails)
               │
               ▼
CORE        Ingest + identity ──► Score ──► Find email ──► Draft (Gemini + checks)
            (normalize, dedupe)   (achievements)  (published pages,   │
                                                  finders, verify)    │
               ┌──────────────────────────────────────────────────────┘
               ▼
REVIEW      Google Sheet · Review tab (approve / edit / reject)
               │ approved rows only
               ▼
SEND        Instantly (API push, or CSV upload) ──► Events (replies, bounces, unsubscribes)
               │                                        │
               ▼                                        ▼
REPORT      Google Sheet · CRM tab ◄── status + suppression updates
            One-pager agent writes booked / attended / sent · Metrics tab weekly
```

**Stack:**

- Python 3.12, managed with `uv`
- SQLite now, Postgres once hosted
- `httpx` for API calls
- Google Sheets and Drive APIs, through a service account
- The Gemini API
- Instantly's API v2

**Commands.** Each one is safe to re-run; it skips work already done.

```
lead-engine import-warm             # pull Excel files from Drive, normalize, de-duplicate
lead-engine source --group engineers --limit 100
lead-engine score
lead-engine find-emails --budget 10  # dollars; stops when the cap is reached
lead-engine draft --group engineers
lead-engine review push|pull         # sync drafts to and from the Review tab
lead-engine send                     # approved rows to Instantly (API or CSV)
lead-engine events                   # replies, bounces, unsubscribes back in
lead-engine crm sync                 # status to the CRM tab
lead-engine report                   # weekly metrics tab
lead-engine run-daily                # everything above in order; supports --dry-run
```

---

## 3. Segments

Each group has its own sources, scoring signals, email strategy and angle. Visas are mentioned only as a general list (O-1, EB-1A, EB-2 NIW), never matched to the person.

| Group | Sources | Signals scored | Email strategy | Email angle |
|---|---|---|---|---|
| Senior engineers | Exa people search ("staff or principal engineer at Google, US"), GitHub for evidence, Exa for patents and talks | Patents, papers, conference talks, notable open-source work, press, seniority | **Personal email only.** Any address on the employer's domain is rejected. If no email is found, the person goes on the manual LinkedIn list | Their patent, talk or project, and long-term options in the US |
| Founders | Exa search over funding news and the YC directory | Recent round and its size, press coverage, investors, prior exits | A work email at their own company is fine | Their recent round or launch |
| Researchers | OpenAlex, Semantic Scholar, arXiv (already built) | Citations, h-index, recent papers, venue, US institution | Address published on their faculty or lab page | A specific recent paper |
| Creators and media | Exa search for press features and awards | Major-outlet coverage, awards, audience size, festival or exhibition history | Business contact published on their own site. Skip if only a manager is listed | A feature, award or recent work |
| Warm list | Excel files on Drive, platform signups | None needed; group assigned from known fields where possible | Email already on file | A consultation offer from someone they've already heard from |

Scoring never uses name, nationality, ethnicity, photos or guessed immigration status. These fields are not collected at all.

---

## 4. Data model

The pipeline uses SQLite tables. Most already exist in `o1-leadgen/db.py`; the new ones are marked.

| Table | Holds | Status |
|---|---|---|
| `person` | Name, employer, group, location, current status | exists |
| `identifier` | Source IDs used to de-duplicate (OpenAlex, ORCID, GitHub, Exa URL, email) | exists |
| `evidence` | Each fact found about a person, with source URL and date. This is the raw material for personalization | exists (as signals) |
| `score` | Versioned score and sub-scores per person | exists |
| `email_lookup` | Every lookup: provider, cost, result, confidence. Enforces the budget cap and prevents paying twice | exists (as ledger) |
| `suppression` | Opted-out, bounced or do-not-contact emails and domains. Checked before every import, draft and send | exists |
| `draft` | Subject, body, evidence used, guardrail results, review decision, reviewer edits | **new** |
| `send` | Instantly campaign, lead ID, step, sent time, method (API or CSV) | **new** |
| `event` | Reply, positive reply, bounce, unsubscribe, booked, attended, one-pager sent, client | **new** |
| `run` | Each command run: counts, errors, cost, duration | **new** |

**Google Sheet.** One spreadsheet with four tabs:

- **Review:** person, group, evidence links, subject, body, a Decision column (Approve, Edit, Reject), and notes. Edited text is pulled back and used exactly as written.
- **CRM:** one row per contacted person, with current status and dates. The one-pager agent updates booked, attended and one-pager sent here.
- **Suppression:** anyone the team marks do-not-contact by hand. It's read on every run.
- **Metrics:** written weekly by `report`.

---

## 5. Lead lifecycle

```
sourced → scored → email found → drafted → approved → sent → replied → booked → attended → one-pager sent → client
```

A lead can exit at any point with one of these statuses: `no email`, `rejected in review`, `bounced`, `unsubscribed`, `not interested`.

Engineers with no personal email go to a **LinkedIn list** instead. Each entry has the profile link, the evidence and a short suggested connection note, for Karishma to send by hand.

---

## 6. Finding emails

Providers are tried in order of cost. Each lookup is recorded, so nobody is looked up twice and spending stops at the cap.

1. **Published on their own pages.** Exa finds the person's personal site, lab page or speaker page, and we read the email they published there. This costs about $1–8 per 1,000 people in Exa API credits.
2. **Exa Websets enrichment,** if Indoko's credits include Websets.
3. **Free tiers:** Hunter, Prospeo, Apollo.
4. **Icypeas** (about $19/month), only if the pilot shows the free routes are too thin.
5. **Verify** every address before it can be drafted. Drop invalid addresses, low-confidence catch-alls, role addresses (info@, hello@) and disposable domains.

**Hard rules:**

- No emails taken from GitHub profiles or commit history.
- No guessed addresses that fail verification.
- Engineers' addresses on their employer's domain are rejected.

---

## 7. Writing emails

Gemini (a current Flash-class model, set in config) gets a structured brief for each person. The brief contains:

- the person's group;
- two or three evidence items, with source URLs;
- the group's template and tone rules;
- the sender details.

Gemini returns JSON with the subject, the body and the evidence item it used.

Every draft passes automated checks before it reaches the Review tab. A draft that fails is regenerated once, then flagged for a person to write.

| Check | Fails when |
|---|---|
| Grounded | The cited fact isn't in the evidence pack, or a number or name in the email doesn't appear in the evidence |
| No eligibility claims | It uses words like "qualify", "eligible", "guarantee" or "approved", or matches a specific visa to the person |
| No sensitive traits | It refers to nationality, ethnicity, country of origin or current visa status |
| Length and form | The body is over 120 words, the subject is over 8 words or misleading, or there is more than one link or more than one ask |
| Footer | The business address or unsubscribe line is missing (the template adds them; this is a backstop) |
| Tone | It contains stock flattery or filler from a banned list |

Follow-ups (steps 2 and 3 of the sequence) are short. They're written in the same pass and reviewed together with the first email.

---

## 8. Review and sending

1. `review push` adds new drafts to the Review tab, grouped by batch.
2. The reviewer sets Decision to Approve, Edit (and changes the text) or Reject.
3. `review pull` reads the decisions. Edited text replaces the draft. Rejected people are not contacted.
4. `send` pushes approved leads to the right Instantly campaign, one campaign per group. The personal subject and body travel as lead custom variables, so the campaign template is just `{{subject}}` and `{{body}}`.
5. Without API access, `send` instead writes a CSV in Instantly's import format and marks those leads as exported. Someone uploads it.
6. `events` pulls replies, bounces and unsubscribes (by API, or from an exported CSV). It updates statuses and adds opt-outs and hard bounces to suppression immediately.

**Instantly's Growth plan holds only 1,000 uploaded contacts.** The sender removes leads whose sequence has finished, which keeps the pilot under the limit. Full volume needs Hypergrowth ($97/month) or a different tool; that decision waits for the pilot.

---

## 9. Deliverability rules

- Suppression is checked at import, before drafting and before sending.
- Each inbox has a daily cap on new contacts. Caps go up in steps, and only while bounces stay under 2% and spam complaints under 0.1%. Sending pauses automatically if either limit is crossed.
- Every email has accurate sender details, an honest subject line, the business address and an unsubscribe line.
- Opt-outs are suppressed on the next `events` run, the same day.
- Nobody who opted out is ever re-imported, including from future warm-list exports.
- Engineers are never emailed at their employer's address.

---

## 10. Reusing existing code

The earlier O-1 sourcing prototype (`~/o1-leadgen`, about 2,350 lines of Python) is the starting point. It moves into this repository first.

| Module | Does | Plan |
|---|---|---|
| `db.py` | Schema, identifiers, signals, scores, ledger, suppression | Keep; add `draft`, `send`, `event` and `run` tables |
| `identity.py` | Matches people across sources without risky auto-merges | Keep |
| `openalex_source.py`, `semanticscholar.py`, `arxiv_source.py` | Researcher sourcing and evidence | Keep |
| `github_engineers.py` | Engineer discovery through GitHub | Keep for evidence; stop collecting emails from it |
| `engine.py`, `scoring.py` | Group-aware scoring | Keep; add founder and creator signals |
| `enrich_db.py` | Pluggable email finder with a budget ledger | Keep; add Exa, Prospeo, Icypeas and a verifier |
| `email_creative.py` | Earlier email drafting | Replace with the Gemini drafter and checks |
| `juicebox_*`, `dol.py`, `employers.py` | Earlier experiments | Leave out of v1 |
| New | Exa source, warm-list importer, Sheets sync, Instantly client, scheduler | To build |

---

## 11. Running it

**Pilot.** It runs on Vivek's Mac:

- A `launchd` job runs `run-daily` each morning: pull review decisions, send approved emails, pull events, sync the CRM.
- Sourcing, scoring and drafting run on demand, one batch at a time.
- Secrets live in a `.env` file that is never committed.
- The SQLite file is backed up to Drive daily.

**After the pilot.** It moves to hosting.

- On Vercel, scheduled runs use Vercel Cron and the database moves to hosted Postgres.
- Long jobs, such as sourcing hundreds of people, are split into small batches, because serverless functions have run-time limits.
- If batching gets awkward, a small always-on host (Railway, Render or Cloud Run) suits a pipeline like this better.
- The code keeps storage and scheduling behind small interfaces, so moving is a configuration change, not a rewrite.

**Monitoring.** Every run writes a row to the `run` table and a one-line summary to a log. If a run errors, hits the budget cap or crosses the bounce or complaint limit, it stops and says why.

---

## 12. Build plan

| Milestone | Contents | Done when |
|---|---|---|
| M0 · Setup | This repo seeded from `o1-leadgen`; config and secrets; Google service account; the Sheet with its four tabs; Instantly connection check | `lead-engine --help` runs and every API key checks out |
| M1 · Warm list | Drive importer, cleaning, de-duplication, suppression, grouping, drafts, Review tab, send, events, CRM sync | First warm batch approved and sent; replies land in the CRM tab |
| M2 · Cold sourcing | Exa source for engineers, founders and creators; researcher sources wired in; scoring for all four groups | 100 scored people per group, with evidence |
| M3 · Emails | Published-page finder, free-tier finders, verifier, budget ledger | Find rate and cost known for each group |
| M4 · Writing | Gemini drafter, the six checks, follow-up steps, LinkedIn list for engineers | Drafts pass the checks; a sample reads well to Karishma |
| M5 · Pilot | Staged sending to 400 people, daily runs, weekly metrics | Four weeks of metrics by group |
| M6 · Hosting | Postgres; scheduled runs on the chosen host | Runs without Vivek's laptop |

M1 ships first, so the warm campaign starts while the cold pipeline is being built. M2–M4 overlap.

---

## 13. Testing

- **Unit tests** cover the parts that protect people and the sending domain: suppression checks, employer-domain rejection for engineers, de-duplication, every draft check, and the budget cap.
- **Recorded API responses** for Exa, Gemini and Instantly let tests run offline and for free.
- **`--dry-run`** works on every command that writes outside the database. It shows what would be sent or written, and does nothing.
- **End-to-end run** to internal addresses before the first real send: draft, approve, send, reply, unsubscribe, bounce. Confirm that each one lands correctly in the CRM and the suppression list.

---

## 14. Risks and open questions

| Item | Why it matters | Plan |
|---|---|---|
| Exa credit type | Websets credits are separate from API credits, so email enrichment may not be covered | Ask Karishma. The published-page finder works with API credits alone |
| Instantly plan | Growth holds only 1,000 contacts and may not include the API | CSV fallback is built in; remove finished leads; decide on an upgrade after the pilot |
| Engineers' personal emails | Likely the lowest find rate of any group | Measure in M3; the LinkedIn list covers the rest |
| Warm-list origin | People must have agreed to hear from Indoko | Confirm before M1 sends; import only confirmed sources |
| Creators' contacts | Often only a manager's address is public | Skip manager-only contacts in v1 |
| Draft quality | Generic or wrong emails hurt replies and reputation | Grounding checks, plus human review of every email in the pilot |
| Laptop as host | Runs stop when the Mac sleeps | The daily schedule catches up on wake; hosting comes in M6 |

---

## 15. What I need to start

- API keys for Exa, Gemini and Instantly, with a note of the Instantly plan.
- Access to the Excel files on Drive, shared with the service account I'll create.
- The sender name and inboxes to use, the business address for the footer, and the booking link.
- Who reviews drafts in the Sheet, and how often.
- Where the warm list came from, so only confirmed contacts are imported.
