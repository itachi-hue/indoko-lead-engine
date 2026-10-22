# ICP sourcing plan: 1,000 immigrant leads a month

Status: draft, 2026-10-20. Updated the same day with M1 results (Exa engineer test). Companion to [DESIGN.md](DESIGN.md); this plan covers *who* we find and how,
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
| Engineers | ~500 | ~2,000 | ~50–65% (M1: 13 of 20) | ~40% |
| Founders | ~300 | ~2,000 | ~25% | ~50%* |
| Researchers | ~200 | ~3,000 | ~7% | ~40%** |
| **Total** | **~1,000** | | | |

\* Founders: about 33% today; higher once we read their company sites for published addresses first.
\*\* Researchers: about 20% today; about 40% expected once we use the email on their papers (M5).

Every rate is an estimate until the tests in section 8 measure it. Each milestone re-checks it.

Odds of reaching 1,000 a month: about 75–80% after M1 (was 50/50). What's still unproven:

- the email confirm rate at engineer companies beyond Apple and Microsoft (M2);
- whether Exa yields about 2,000 *distinct* engineers a month (M3);
- the founder filter (M4);
- inbox warm-up time.

Expected outcome at 1–3% positive replies: about 10–30 conversations a month.

## 4. Immigrant filter

**Rule:** we infer a move only from **places in someone's record**: where they studied, worked or
filed patents. We also accept **what they said about themselves**. We never use names, ethnicity or
photos.

| Signal | Source | Used for | Strength |
|---|---|---|---|
| Career started abroad, first US institution within 10 years | OpenAlex affiliation history (free) | Researchers | Built: about 1 in 15 researchers |
| Older patents filed from abroad, recent ones from the US | PatentsView inventor addresses (free) | Engineers | Built, untested live; low recall, because most immigrants came as students |
| First school or first job outside the US, now working in the US | Exa people search: `educationHistory` (school; country looked up free in OpenAlex institutions) and `workHistory` (job locations and dates) | Engineers, founders | **M1 passed**: 13 of 20 engineers. **Main engine** |
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
  - `moved_within_years = 10` for researchers
  - **15 years for engineers**: Indian and Chinese engineers who came around 2010–2015 are often still
    in green-card backlogs, which is exactly where EB-1A or NIW helps.

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

Revised 2026-10-20 after M3, using measured costs.

**By group:**

| Group | Leads a month | Exa | Email checks |
|---|---|---|---|
| Engineers (13 companies) | ~600 | ~$5 (~$0.0013 per profile; ~4–5 profiles per lead; evidence searches off) | ~3,000 |
| Researchers | ~200 | ~$5 (OpenAlex is free; Exa only for faculty pages) | ~500 |
| Founders | ~200 | ~$30 (news articles plus one profile lookup per founder) | ~1,000 |
| **Total** | **~1,000** | **~$40** | **~4,500** |

**By item:**

| Item | Monthly | Who pays |
|---|---|---|
| SEC Form D, PatentsView, OpenAlex, Europe PMC | $0 | Free |
| Exa | ~$40 | Karishma's existing Exa credits |
| Reoon (~4,500 checks) | ~$10–50 (price to confirm) | **New**; the paid plan is required, the free plan is daily |
| Gemini drafting | ~$1–5 | Her existing account |
| Instantly, inboxes, domains, hosting | – | Already paid |

**Total: about $50–100 a month (5–10 cents per lead). Only Reoon is new money.**

**What could move it:**

- The engineer email confirm rate (assumed ~50%, not yet measured). At 25%, add about $10–25.
- Founders are the least tested group; their Exa cost could be off by 2×.

Either way it stays under $150 a month.

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
| **M1** ✅ | Exa people search on 20 Microsoft and Apple engineers: does it return education history, and what share studied abroad? | – | ~$0.03 | **Passed:** education returned for 20 of 20; 13 of 20 studied or worked abroad first (7 first came to the US in 2018 or later) |
| **M2** ✅ | Check 50 mid-size tech companies for catch-all mail servers; keep the ones that confirm mailboxes | – | 48 Reoon checks | **12 of 48 confirm** (+ Apple from earlier) = 13 companies. Below the ≥20 gate, but they include the biggest engineering employers. See section 14 |
| **M3** | Engineer source: Exa people search + education and job-location filter across the M2 companies; 100 engineers end to end | M1 pass ✅ | ~$1 | ≥30 leads from 100 |
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
| Exa people results don't include education | Resolved by M1: 20 of 20 returned education and job history |
| Exa returns people who no longer work at the searched company (M1: 6 of 20) | Check the current employer from `workHistory` (the entry with no end date) |
| Vague job locations ("Hybrid", a city with no country) and schools with no country in OpenAlex | Better location matching, a list of US cities, and a fallback lookup of the school's country; never guess from names |
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
| Reoon paid plan | **Needed now for any burst:** the free plan is a *daily* allowance (it ran out after ~134 checks on 2026-10-20) | Needed before M3/M4 email checks |
| Rotate the Reoon and Exa keys (both were pasted in chat) | Security | Not done |

## 12. M1 results (2026-10-20)

**Test:** two Exa people searches ("staff software engineer at Microsoft", "principal software engineer
at Apple"), 10 results each. Cost about $0.03.

**What came back:** each result carries a person entity with:

- `workHistory`: title, company, city and country, start and end dates;
- `educationHistory`: school, degree, dates. No country, so we look the school up in OpenAlex.

**Results:**

| Measure | Result |
|---|---|
| Education history present | 20 of 20 |
| First school or first job outside the US | 13 of 20 (65%) |
| …of those, first US job in 2018 or later | 7 |
| …first US job 2009–2015 | 4 |
| Currently at the searched company | 14 of 20; the others were at Stripe, Shopify, GitHub and others |

**Countries seen:** India, Romania, Russia, Iran, Egypt, Sri Lanka and Belarus.

**Caveat:** the sample was small. The rate at scale is probably lower than 65%, but it has a wide margin
over the 20% gate.

## 13. Spend to date (2026-10-20)

| Item | Usage | Cost |
|---|---|---|
| Exa | About 95 results, about $1.60 in total | Karishma's credits |
| Reoon | About 100 checks | $0 (free plan, ~600 a month) |
| OpenAlex | About 300 researchers scanned | $0 |

**Breakdown of the Exa results:**

- 30 engineer profiles: 10 sourced, 20 for the M1 test;
- 20 news articles for founders and creators;
- about 44 searches for people's public pages, to find emails.

**What we have:** 26 people from the first test, 20 researchers who moved here recently, and 13
engineers in the M1 test sample.

**Confirmed emails:**

- 7 confirmed mailboxes from the first test, plus 2 that the person published themselves;
- 2 engineer work addresses, from the switch to work addresses;
- 3 more researchers, from the move filter.

Most of the Exa spend (~$1.25) was the first sourcing test, which read whole news articles. People
search costs about $0.0015 per person.

## 14. M2 results (2026-10-20)

**Method:** `lead-engine check-domains` sends Reoon one made-up address per company. A server that
rejects it confirms mailboxes; a server that accepts it accepts every address.

| Result | Companies |
|---|---|
| **Confirms mailboxes (use)** | Microsoft, NVIDIA, LinkedIn, Tesla, Intel, Intuit, eBay, Cloudflare, MongoDB, Figma, Plaid, Rippling, plus Apple (confirmed real addresses earlier; the made-up address got no answer) |
| Accepts every address (skip) | Netflix, OpenAI, Stripe, Databricks, Uber, Airbnb, Snowflake, Adobe, AMD, Cisco, IBM, PayPal, Workday, Pinterest, Dropbox, DoorDash, Lyft, Coinbase, Robinhood, Datadog, Twilio, Okta, Atlassian, Palantir, Snap, Roblox, Instacart, Ramp, Anthropic, Scale AI |
| No answer | Apple (see above), Salesforce, Qualcomm, Oracle, ServiceNow, Zoom |
| Not checked (Reoon ran out) | Expedia, Zillow, Wayfair, Etsy, Splunk, Broadcom, Samsung, Visa, Block |

**What it means:**

- Only about a quarter of companies can have a guessed work address confirmed.
- Engineers now come from 13 companies (`sourcing.engineer_companies`).
- Microsoft, Intel, NVIDIA, Apple and Tesla alone employ tens of thousands of US engineers, so the
  pool should be large enough. M3 measures how many distinct people Exa returns.

**Reoon:** the free plan is a daily allowance, not 600 to use at once. It hit zero after ~134 checks
on 2026-10-20. Pilot volume needs the paid plan.

## 15. M3 results and lessons (2026-10-20)

**M3 (partial):**

- Found 18 engineers who moved here, for $0.92. The run was stopped once it stalled.
- All 18 were at Apple, because of an ordering bug: searches now cycle through every company first.
- The extra evidence searches (patents, talks, personal site) found something for only 6 of 18, at
  about 10× the cost. They are now off by default (`sourcing.engineer_evidence_searches = false`).
  That cuts the cost from about $0.05 to about $0.005 per engineer.
- The search position is now saved after every search, so a stopped run resumes where it left off.
- Emails for the 18 are still to check. This needs Reoon credits.

**Reoon usage, 134 checks:**

| What | Checks |
|---|---|
| Wasted on catch-all servers (24 founder guesses, 24 Google/Meta/Amazon guesses) | 48 |
| Company mail-server check (M2) | 48 |
| Real confirmations | 14 |
| Rejected or no answer | 24 |

**What we fixed and learned:**

- **Wasted checks:** guessing now stops at the first catch-all answer, and catch-all companies are
  excluded. Each engineer now needs about 1–3 checks instead of 6.
- **Daily allowance:** Reoon's free plan is a daily allowance, so a burst run needs the paid plan.

**Status of the 37 people who passed the immigrant filter today:**

- 4 have confirmed emails (researchers).
- 18 Apple engineers are waiting for Reoon credits.
- 15 researchers are waiting for a re-run with the paper-email finder.
- Expected: about 15–20 of the 37 end with a confirmed email.

**Full list:** `~/indoko/leads-2026-10-20.csv` (local only; it contains personal data).

## 16. Sourcing results (2026-10-22)

| Group | Found (moved to the US) | Cost each | Speed | Notes |
|---|---|---|---|---|
| Engineers | 20 more (37 in total) across Intuit, Intel, Tesla, LinkedIn, NVIDIA, Microsoft, Apple | ~$0.011 | 20 in 11 s | Search order now cycles companies |
| Founders | 32, each with a raise confirmed by a headline (e.g. Metriport $26M, Deep Cogito $43M, Plural $7M) | ~$0.05–0.09 | 20 in ~2 min | Most of the cost is funding searches for founders who passed the immigrant filter but had no recent raise |
| Founder company domains | 24 of 32 found (75%) | $0 | | e.g. metriport.com, e2b.dev, nango.dev |

**Founder funding rules, after three live corrections:**

- The headline must announce a raise, and must not be hedged ("could attract investors").
- The company match ignores spacing ("StackAI" = "Stack AI").
- The article must name the founder, **or** the headline must have the company as its own name directly
  followed by a raise verb.
  - This rejects "Grizzly Discoveries raised" (a mining company, not Grizzly) and "…automate Month-End
    Close" (a different company).
- Credentials and nicknames are stripped from names ("Julius Bruch, MD, PhD" → "Julius Bruch").

**OpenAlex rate limit:**

- After a day of anonymous testing, OpenAlex blocked us for about 1h45m.
- Long blocks (over 5 minutes) now fail the call immediately; previously each lookup waited 4×120 s
  and the run stalled.
- During a block, school countries come from the school's name.
- Fix for production: a free `OPENALEX_API_KEY`.

**Revised founder cost:** about $0.05–0.09 per founder, so 200 founders a month is ~$10–18 of Exa.
That's below the $30 in section 6.

**Blocked on Reoon credits:** confirming the emails of 37 engineers and 24 founders.
