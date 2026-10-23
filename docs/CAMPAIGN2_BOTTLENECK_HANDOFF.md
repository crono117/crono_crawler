# Crono Crawler Campaign 2 — Current Bottleneck Report

**Snapshot:** October 1, 2026 at 8:13 AM PDT / 3:13 PM UTC
**Campaign:** 2 — Real-source discovery pilot
**Live checkout:** `/home/crono/dev/crono_crawler-deploy-b2161bc`
**Live commit:** `b2161bc7a32b987f9bf411d260eae9ca5dbc466d`
**Production database:** `/home/crono/dev/crono_crawler/data/leads.sqlite3`

## GitHub agent handoff

This snapshot is published for agent work at:

- **Repository:** `https://github.com/crono117/crono_crawler`
- **Branch:** `handoff/campaign2-bottlenecks-2026-10-01`
- **Code base commit:** `b2161bc7a32b987f9bf411d260eae9ca5dbc466d`
- **Branch purpose:** A stable handoff point for agents to investigate and implement the Campaign 2 bottlenecks documented below.
- **Tracked report path:** `docs/CAMPAIGN2_BOTTLENECK_HANDOFF.md`

Checkout commands:

```bash
git fetch origin handoff/campaign2-bottlenecks-2026-10-01
git switch --track origin/handoff/campaign2-bottlenecks-2026-10-01
```

The production service is still running the base code commit `b2161bc7a32b987f9bf411d260eae9ca5dbc466d`. The documentation commit on the handoff branch is **not deployed**. Agents must work in isolated branches or worktrees based on this handoff branch and must not modify production, production data, service configuration, budgets, approvals, credentials, or remote branches without separate authorization.

## Executive summary

The acquisition bottleneck has moved. Brave search is now enabled, has a configured key, and consumed Campaign 2's full daily search allowance. It successfully introduced new companies into automatic setup, but none converted into an active source or a production lead.

The current bottleneck is **intake precision and automatic-setup conversion**, not worker health, URL capacity, fetch budget, or absence of search results.

```text
Brave search finds companies
             │
             ▼
Generic homepages pass because page intent is optional
             │
             ▼
All 25 daily automatic-setup slots are consumed
             │
             ├── 18 paused
             └── 7 failed
             │
             ▼
0 active sources
0 new observations
0 new production leads
```

## What changed

Brave source search is now operational. This occurred after the prior audit and was not changed during the read-only diagnosis behind this report.

Current search state:

- Provider enabled: **yes**.
- API key present: **yes**.
- Campaign search enabled: **yes**.
- Generated queries enabled: **yes**.
- Query pool: **16**.
- Campaign searches used today: **5 / 5**.
- Global searches used today: **5 / 20**.
- Search-derived active URLs in the trailing 24-hour inventory: **17**.
- Next search eligibility: **October 2, 2026 at 00:00 UTC**.
- Latest search status: queued and deferred by the daily campaign search allowance.

The new-origin acquisition blocker is therefore no longer the primary problem.

## Today's automatic-setup cohort

Brave-backed discovery and existing policy execution created the full daily cohort:

- New sources: **25**.
- New automatic setup jobs: **25 / 25 daily allowance**.
- Active: **0**.
- Paused: **18**.
- Failed: **7**.
- New observations: **0**.
- New non-synthetic production leads: **0**.

Campaign-wide automation state is now:

- Total jobs: **70**.
- Active: **2**.
- Paused: **50**.
- Failed: **18**.

The two active sources predate today's cohort. None of today's 25 sites became active.

## Primary issue: search results are not landing on lead-bearing pages

The current automation policy allows a generic company homepage to enter setup:

- `require_page_intent=false`.
- `min_url_score=70`.
- `max_sites_per_day=25`.

Current pending pool:

- Pending pages: **322**.
- Pending sites: **234**.
- Intent-optional admissible sites: **177**.
- Intent-required admissible sites at score 70: **0**.
- Intent-required admissible sites at score 50: **0**.
- Intent-required admissible sites at score 35: **1**.

This means the system has company URLs, but almost no URLs that already resemble direct team, staff, leadership, representative, agent, reseller, directory, or person-profile pages.

Strictly requiring page intent would currently starve the setup queue. Leaving it optional fills the queue with generic sites that usually fail later.

## Failure profile

Current durable setup outcomes across all 70 jobs:

- No people page: **28**.
- Cards without usable sales records: **9**.
- Access failures: **16**.
- Redirect-scope outcomes: **14**.
- Other setup errors: **1**.
- Active canary-passed sources: **2**.

Detailed evidence includes:

- `no_people_listed`: **22**.
- People without personal contacts: **9** across people-page and card-layout outcomes.
- Unavailable pages: **8**.
- Robots returned HTML: **4**.
- Access challenges: **2**.
- Cards without people: **3**.
- HTTP 401/403: **1**.
- Oversized response: **1**.

The typical failure is not that the worker cannot execute. It is that the selected site or page does not publish a named sales person with evidence-backed contact information.

## Daily gates now blocking additional attempts

At the snapshot time:

- Automatic setup jobs created today: **25 / 25**.
- Campaign searches used today: **5 / 5**.
- Latest search deferred until the next UTC day.

These controls reset on the next UTC day. Increasing them would not fix the current conversion problem; it would feed more low-quality sites into the same failure funnel.

## What is not blocking the system

### Worker and service

- Lead service: active.
- Loopback console: HTTP 200.
- Worker lease: fresh.
- Exactly one production worker owns the database queue.

### URL capacity

- Active inventory: **1,919 / 3,000**.
- Archived inventory: **2,799**.
- Parkable origin excess: **0**.
- Capacity is available.

### Fetch budget

- Requests used today: **849 / 5,000**.
- The campaign fetch budget is not close to exhaustion.

### Crawl queue

- Open discovery runs: **0**.
- There is no stuck open run preventing work.

## Inventory quality remains a secondary issue

Trailing 24-hour inventory:

- URLs created: **1,738**.
- Still counted: **1,005**.
- Sitemap-derived: **629**.
- Collector-derived: **178**.
- Link-derived: **120**.
- Seed-derived: **61**.
- Search-derived: **17**.

Freelancer remains dominant:

- Freelancer currently contributes **506** active rows.
- It accounts for **492** counted rows in the trailing 24-hour view.

The per-origin cap prevents additional unprotected excess, but protected historical rows remain. This is not the immediate blocker, because active inventory still has substantial free capacity.

## Current production result

- Non-synthetic production leads: **14**.
- Demo-only leads: **7**.
- Observations: **21**.
- New production leads today: **0**.

No source links are listed because no new lead was created.

## Current bottleneck statement

**Automation is running, but it is automating low-precision intake.**

The system can now:

1. Generate search queries.
2. Call Brave.
3. Discover companies.
4. Create bounded automatic-setup jobs.
5. Probe, classify, validate recipes, and record durable outcomes.

It cannot yet reliably select lead-bearing team/person/contact pages before spending the daily setup allowance.

## Planning implication

The next change should focus on:

1. Search-result precision toward direct team/profile/contact pages.
2. Multi-result site expansion before setup, so a company homepage can be resolved to a high-intent internal page.
3. Ranking daily setup candidates by direct intent, origin diversity, and historical source/query yield.
4. Reserving daily setup slots for high-confidence candidates rather than first-eligible homepages.
5. Learning from durable outcomes such as `no_people_listed`, access failure, and people-without-personal-contact.

The next move should **not** be higher search quotas, higher setup limits, lower thresholds, or additional fetch budget. Those changes would increase volume without addressing the zero-conversion cohort.

## Safety boundary

This report is read-only. Preparing it did not change:

- Campaign configuration.
- Automation policy.
- Search or fetch quotas.
- Sources or approvals.
- Brave configuration.
- Jev configuration.
- Queues, jobs, leads, or observations.
