# Intake ranking handoff

| | |
|---|---|
| Base (live) | `b2161bc7a32b987f9bf411d260eae9ca5dbc466d` (intake precision) |
| Branch | `work/campaign2-intake-precision` (from `handoff/campaign2-bottlenecks-2026-10-01`) |
| Schema | `automation.0006_intake_ranking`, `discovery.0010_intake_ranking`: additive, reversible |
| Live database | Not read or changed by this work. Nothing here was run against production. |

## Problem (Campaign 2 bottleneck report, 2026-10-01)

Brave search works, but the cohort converted at **0 of 25**: every new site that took a daily setup slot ended paused (18) or failed (7). Across all 70 jobs, 2 are active. The report's diagnosis, confirmed in code:

1. `require_page_intent=false` lets a bare homepage in, and `policy.consider()` was called for each search result *as it registered*. The first 25 generic homepages took the day's slots, ranked by nothing.
2. Requiring intent instead starves setup (0 admissible sites at score 70/50, 1 at 35), because search results rarely land on team pages.
3. Nothing used the 70 durable outcomes (`no_people_listed`, access failures, ...) when choosing the next sites or queries.

Raising searches, setup limits, fetch budget or lowering thresholds would only feed more volume into the same funnel, so none of those are changed.

## Change

All new behavior is **off until an operator sets it**: `explore_share` defaults to 100 (historical first-eligible behavior) and `search_precision` defaults to off. The only change at deploy time is that new jobs record why they were admitted.

- **Tiered, ranked admission** (`automation/intake.py`, `SitePolicy.explore_share`). Candidates are `direct` (team/staff/people/agents/reps in path or label), `company` (about/contact) or `generic` (a homepage). With `explore_share < 100`, at most that percent of `max_sites_per_day` (rounded up) may go to non-direct candidates; the rest stay reserved for direct pages. Non-direct pages are no longer admitted at registration; a ranked pass (worker maintenance tick, at most once a minute) admits the best-ranked ones.
- **One start page per site.** All of a site's pending pages compete, so a site with a pending `/about-us` starts there, not at `/`.
- **Ranking.** Tier base (300/200/100) + campaign score + origin page-yield + the search query's setup history + the company family's durable outcomes. Each adjustment is bounded and written to `SiteAutomationJob.admission_reasons`.
- **Diversity.** One non-direct site per company family per UTC day. Subdomains share a family (`blog.acme.com` and `www.acme.com`); tenants of shared hosts (`*.wixsite.com`, `*.github.io`, `*.myshopify.com`, ...) do not. This is a heuristic with no public-suffix list; it only affects ranking and de-duplication of exploratory slots, never authorization.
- **Outcome learning** (`automation/learning.py`).
  - Per query: sites admitted from it that reached contacts vs. durable misses (`no_people_listed`, `cards_without_people`, `people_without_personal_contacts`, `contacts_rejected`, `no_pages`, and access outcomes). Merged into the existing Thompson-sampled query choice with page yield. Unavailable pages, redirect-scope, canonical conflicts and `setup_error` describe our transport/scope handling, not the query, and are **not** counted.
  - Per company family: `-60` if a sibling site refused automated access, `-20` for no usable people, `+15` if it produced contacts.
  - Nothing here authorizes, fetches, dismisses or widens scope.
- **Search precision** (`Campaign.search_precision`, form field on the source-search page). With generated queries on, the pool becomes `"<industry>" intitle:<team|staff|people|agents|representatives> ["<region>"]` instead of `"our team"/"sales team"/"contact"` intents. Bare `contact` is company-page intent, never direct intent. Exact queries are never rewritten. Brave documents `intitle:` as an experimental operator: check real results before trusting it (see "Not done").
- **Diagnostics** (read-only, no network):
  - `manage.py automation_intake --campaign N [--explore-share S]`: pool by tier, slots, who would be admitted now and why, the admission funnel by tier/method and by query.
  - `automation_audit` adds `pending_pool.admissible_sites_by_intake_tier` and `admission_funnel`.
  - `discovery_inventory` adds `top_origin_protection`: for the top origins, how many counted rows are protected (seed, import, manual review, sitemap, active job, produced contacts, follow-up, setup source, contact evidence, start URL) vs `parkable`. This explains why a large origin (Freelancer) stays large after `--origin-cap`.
- **Setting.** `manage.py automation_policy --explore-share N` (dry run unless `--apply`) changes it without pausing any job; `explore_share` is an admission field (`policy.ADMISSION_FIELDS`).

Unchanged: approvals, source bindings, scope, robots, the probe/recipe/canary gates, `min_recipe_score`, budgets and quotas, Brave and Jev configuration. The daily `max_sites_per_day` still caps everything, and `consider()` re-checks every gate under the campaign lock.

## Expected effect (be realistic)

The report's cohort converted at 0/25 and the whole funnel at 2/70 (about 3%). With `explore_share=20` and 25 slots, at most 5 non-direct sites per day are set up until precision queries start producing direct pages, so **setup volume drops by design**. Reserved slots simply go unused on days with few direct pages. If that is too little exploration to learn from, raise the share; the preview shows the trade.

## Verification

```
manage.py check                              no issues
manage.py makemigrations --check --dry-run   No changes detected
manage.py test                               Ran 681 tests ... OK   (643 before; 38 new)
migrate automation 0005 / discovery 0009     unapply and reapply OK (throwaway DB)
smoke_local, smoke_jev, smoke_inventory, smoke_extraction, run_smoke_bulk_import   all PASS, exit 0
```

New tests (`automation/tests/test_intake.py`, `discovery/tests/test_protection_breakdown.py`) cover: tier classification; the exploratory cap and rounding; zero share; legacy share 100 unchanged; best page per site; family diversity incl. shared hosting; the daily cap in ranked mode; query/family learning and which outcomes are ignored; precision query pool; the policy command (dry run, apply, bounds, jobs untouched); the preview matching what the allocator then does; and a regression test for ranked-window starvation (verified to fail without the fix).

Offline ranking cost on synthetic pools: 0.2 s for 322 pending pages, 1.2 s for 2,000, once a minute on the worker tick. Mock-only: no live Brave request or real site was contacted.

## Operator sequence (gate each step on the previous data)

Commands run from the deployed checkout, campaign 2. Setup slots reset on the UTC day, so changes show their effect from the next UTC day.

1. **Deploy** as in `docs/DISCOVERY_CAPACITY_HANDOFF.md` (backup, migrate, check, collectstatic, switch, verify one worker). Behavior is unchanged except that new jobs record admission metadata.
2. **Preview, read-only:**
   ```bash
   manage.py automation_intake --campaign 2 --explore-share 20 > intake.json
   manage.py automation_audit --campaign 2 --no-jobs > audit.json
   manage.py discovery_inventory --campaign 2 > inv.json
   ```
   Check `admissible_sites_by_tier` and `would_admit`. If `direct` is empty and every candidate is generic, step 4 matters more than step 3.
3. **Set the share:**
   ```bash
   manage.py automation_policy --campaign 2 --explore-share 20            # dry run
   manage.py automation_policy --campaign 2 --explore-share 20 --apply
   ```
4. **Precision queries.** Turn on "Generated queries target team/people page titles" on the campaign's source-search page. Spot-check the first batch of results: do the titles actually name team/people pages?
5. **Observe one UTC day.** Record jobs by tier and outcome (`automation_intake` funnel), which queries admitted which sites, and new non-synthetic leads.
6. **Tune** `--explore-share` from the funnel. Origin share for Freelancer is a separate decision (`discovery_inventory --origin-cap`, see `INTAKE_PRECISION_HANDOFF.md`); read `top_origin_protection` first, since protected history is never parked.

## Rollback

- **Setting:** `automation_policy --explore-share 100 --apply` (restores first-eligible admission). Turn the search-precision checkbox off to restore the old query pool.
- **Code:** stop the service, point it at the `b2161bc` checkout, restart. The additive columns stay and older code ignores them.

## Not done / limits

- **No search-based site expansion.** Resolving a homepage into a team page is done only from already-found pages plus the existing in-setup probe (which already follows about/contact/team links). A `site:domain intitle:team` lookup per company would spend the 5-per-day search allowance, which the report says not to raise, so it was left out. If wanted, it should draw from its own capped budget.
- **`intitle:` is unverified live.** Brave's operators are documented as experimental and were not exercised against the live API. If results are worse than the old pool, turn the checkbox off.
- **Learning is shallow.** Family and query adjustments use counts of durable outcomes only (bounded, minimum evidence of 2 sites for a query); there is no per-industry or per-page-template model.
- **Freelancer inventory** is diagnosed (`top_origin_protection`) but not changed.
- **Recipe floor** (`min_recipe_score` 70 vs 85) is untouched and remains a separate decision.
