# ICP sourcing plan: 1,000 immigrant leads a month

Status: draft, 2026-10-20. Companion to [DESIGN.md](DESIGN.md); this plan covers *who* we find and how,
DESIGN.md covers how the pipeline runs.

## 1. Goal

**1,000 new people a month** who:

1. fit the ICP (below),
2. pass the immigrant filter (section 4), and
3. have an email address the verifier confirmed (section 5).

"Lead" in this doc always means all three.

## 2. ICP

People with the kind of record that EB-1A, O-1 and EB-2 NIW reward (funding, patents, citations,
senior roles, press), who **moved to the US recently**.

| Group | Priority | Who |
|---|---|---|
| Engineers | Main | Staff/principal engineers and inventors at US tech companies, **not Google, Meta or Amazon** (their mail servers accept every address, so we can't confirm one) |
| Founders | Main | Founders of US startups that raised money in the last 12 months |
| Researchers | Supporting | Postdocs and early-career faculty at US institutions; free to source, strong fit for NIW |

Out of scope: federal and state employees (most of those jobs require citizenship), anyone outside the US.

## 3. Target mix and funnel

| Group | Leads a month | Found a month | Pass immigrant filter | Confirmed email |
|---|---|---|---|---|
| Engineers | ~500 | ~3,500 | ~35% | ~40% |
| Founders | ~300 | ~2,000 | ~25% | ~50%* |
| Researchers | ~200 | ~3,000 | ~7% | ~40%** |
| **Total** | **~1,000** | | | |

\* Founders: about 33% today; higher once we read their company sites for published addresses first.
\*\* Researchers: about 20% today; about 40% expected once we use the email on their papers (M5).

Every rate is an estimate until the tests in section 8 measure it. Each milestone re-checks it.

## 4. Immigrant filter

**Rule:** we infer a move only from **places in someone's record**: where they studied, worked or
filed patents. We also accept **what they said about themselves**. We never use names, ethnicity or
photos.

| Signal | Source | Used for | Strength |
|---|---|---|---|
| Career started abroad, first US institution within 10 years | OpenAlex affiliation history (free) | Researchers | Built: about 1 in 15 researchers |
| Older patents filed from abroad, recent ones from the US | PatentsView inventor addresses (free) | Engineers | Built, untested live; low recall, because most immigrants came as students |
| First degree from a university outside the US, now working in the US | Exa people search (education history) | Engineers, founders | **To test (M1)**: expected to be the main engine |
| Bio says they studied or worked abroad before coming to the US | Exa reading founder bios and company pages | Founders | To build (M4) |
| Backed by a fund or program for immigrant founders | Portfolio pages (e.g. Unshackled Ventures) | Founders | To build (M4) |
| Posted about their own O-1, EB-1A or green-card process | Exa search | Anyone | To build (M6); small but high intent |

**How a move is stored:**

- As the `moved_to_us` signal (`sources/mobility.py`).
- With **no evidence text**, so a draft can never quote it.
- Emails mention the person's work, never where they came from. The `no_sensitive` draft check
  enforces this.
- Config (`[sourcing]`):
  - `moved_to_us = "require" | "prefer" | "off"`
  - `moved_within_years = 10`

**A move is a signal, not proof.** Someone who moved in 2017 may already hold a green card, so the
email still lets the reader decide whether it applies.

## 5. Email finding and verification

1. **Published address:** on the person's own page, faculty page, company site, or (researchers)
   their papers as corresponding author.
2. **Pattern guess at the employer's domain:**
   - Engineers: only addresses that contain both the full first and last name.
   - Founders: the usual patterns.
3. **Verify with Reoon** (power mode). Rules:
   - A guess is used only if Reoon confirms the mailbox.
   - A catch-all result stops guessing on that domain.
   - A published address on a catch-all domain is kept.

**Rejected at find time and again at send time:**

- role addresses;
- suppressed addresses;
- any engineer at a corporate domain other than their own employer's;
- foreign country or university domains.

**Engineer company list:** before adding a company, verify one deliberately fake address there
(1 check). If the server accepts it, the company is catch-all, so we leave it out. Config:
`sourcing.engineer_companies`.

## 6. Cost (1,000 leads a month)

| Item | Monthly | Who pays |
|---|---|---|
| SEC Form D, PatentsView, OpenAlex | $0 | Free |
| Exa people search, bio reading, fund portfolios | ~$150–250 | Karishma's existing Exa credits |
| Reoon verification (~8,000–10,000 checks) | ~$40–80 | **New** |
| Gemini drafting | ~$1–5 | Her existing account |
| Instantly, inboxes, domains, hosting | – | Already paid |

**New spend: about $40–80 a month.**

Sending capacity: 1,000 people plus 2 follow-ups is about 3,000 emails a month. At about 30 a day per
inbox, that needs about **5 warmed-up inboxes**.

## 7. Compliance

- **Recipients:** US-based recipients only.
- **CAN-SPAM:** footer on every email, and unsubscribes honoured through the Suppression tab.
- **Drafts:** no claims about eligibility, and no mention of nationality, ethnicity or immigration
  status. Enforced by the draft checks.
- **Engineers:** never emailed at another company's domain. Work addresses at their own employer
  are allowed (`emails.engineer_policy = "work_address_ok"`, chosen 2026-10-20).
- **Approval:** a person approves every draft in the Review tab before it is sent.

## 8. Milestones

Each milestone ends with measured rates and a go/no-go decision. Spend is from Karishma's Exa
credits unless noted.

| # | What | Needs | Spend | Gate |
|---|---|---|---|---|
| **M1** | Exa people search on 20 Microsoft and Apple engineers: does it return education history, and what share studied abroad? | – | ~$1 | Education returned for ≥70% of people, and studied-abroad share ≥20%. Otherwise engineers fall back to patents and the target drops |
| **M2** | Check 50 mid-size tech companies for catch-all mail servers; keep the ones that confirm mailboxes | – | ~50 Reoon checks | ≥20 usable companies |
| **M3** | Engineer source: Exa people search + education filter across the M2 companies; 100 engineers end to end | M1 pass | ~$3–5 | ≥30 leads from 100 |
| **M4** | Founders: SEC Form D live; immigrant signal from bios and immigrant-founder fund portfolios; 100 founders end to end | SEC contact email | ~$3–5 | ≥20 leads from 100 |
| **M5** | Researchers: corresponding-author emails from papers; 200 movers | – | $0 | Email rate ≥35% |
| **M6** | People posting about their own visa process: 20 people | – | ~$1 | ≥5 usable leads |
| **M7** | Engineers via patents with the move filter, live | PatentsView key | $0 | Measure only (supplement) |
| **M8** | First real week: draft, review, send 10 a day; track replies | Gemini key, Sheet access, Instantly | – | Positive-reply rate, to set the scale-up decision |

After M3 and M4 we re-run the section 3 math with measured rates. If the total falls short of 1,000,
the levers in order are:

1. add more engineer companies;
2. widen the founder window (to 18 months, and Series A/B);
3. raise researcher volume;
4. switch `moved_to_us` to `prefer` and rely on the email's wording to filter.

## 9. Metrics (weekly, Metrics tab)

- **Funnel by group:** found → passed immigrant filter → confirmed email → approved → sent.
- **Spend:** Exa credits and Reoon checks per lead.
- **Bounce rate.** Sending auto-pauses above 2%.
- **Replies:** positive-reply rate by group and by immigrant signal, to learn which signal predicts
  replies best.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Exa people results don't include education | M1 tests it first; fallback is patents plus bios |
| Merged OpenAlex profiles (two people as one) | Foreign-domain rule; human review of each draft in the Review tab |
| Work addresses get lower replies, and employer IT can see the email | Prefer personal published addresses where found; measure replies by address type |
| Mid-size companies turn catch-all | Re-check the company list monthly (M2 script) |
| Immigrant signal misses people who are already citizens | Wording lets the reader self-select; track replies by signal |

## 11. Inputs needed

| Item | Unlocks | Status |
|---|---|---|
| SEC contact email | M4 | Waiting on Vivek |
| PatentsView key (free) | M7 | Waiting on Vivek |
| Gemini key + Google service account with Sheet access | M8 | Waiting |
| Reoon paid plan | When monthly checks pass ~600 | Later |
| Rotate the Reoon and Exa keys (both were pasted in chat) | Security | Not done |
