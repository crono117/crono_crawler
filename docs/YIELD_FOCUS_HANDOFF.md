# Yield focus handoff (branch fix/yield-focus, base b2161bc)

Addresses the Hermes report of 2026-09-30: 14 leads, 0 individual contacts, ~3,400 marketplace URLs at 0% yield,
399/500 queued jobs skipped per run, bank results from generic queries, unexplained "failed" runs, stuck jobs on blocked sources.

## Code changes (no migration)
- `ranking`: new exclusion rule `host:<word>` — matches a hostname label part equal to or ending in the word
  (`host:bank` hits pncbank.com, first-bank.com; not bankcardusa.com, not a `.bank` TLD, never page prose).
- `acquisition.query_pool`: generated intents are now rep-focused (`independent sales agent`, `meet our team`,
  `sales representative`, `agent program`, `<role> team`); bare `contact` is gone; `host:` exclusions become `-word`.
- `services.queue_candidate`: the per-run sitemap allowance (5/source) and zero-yield page allowance (5/origin once
  10+ fetched pages with no contacts) now apply at **queue** time, so those jobs no longer take max_pages slots.
  Processing-time checks are unchanged. Seeds, exact starts and company follow-ups stay exempt.
- `services.finish`: every finished run gets a message — job counts by status plus top 3 failure and skip reasons.
  Status semantics unchanged ("failed" if any job failed).
- New `campaign_tuneup` command (dry run unless `--apply`; no network; does **not** touch SitePolicy, so no setup job pauses).

Tests: 654 pass (643 + 11 new in `discovery/tests/test_yield_focus.py`); check/drift clean; smokes pass.

## Rollout (Lu authorizes each live step)
1. Isolated build/test at this commit, same as previous handoffs.
2. Deploy: stop, backup (backup API), switch checkout, `migrate` (no-op), `check`, restart, verify one worker.
3. Dry runs on live (read-only), report the JSON:
   ```
   manage.py campaign_tuneup --campaign 2 --block-banks --clear-blocked
   manage.py campaign_tuneup --campaign 2 --pause-zero-yield --min-fetched 50 \
     --pause-host salesfolks.com --pause-host freelancer.com --pause-host commissionpeople.com \
     --pause-host vendux.com --pause-host builtin.com
   manage.py campaign_tuneup --campaign 2 --queries deploy/campaign2_rep_queries.txt
   ```
   Confirm floridamerchantservice.com and echelonpayments.com are NOT in `paused_sources`. Add any missing bank
   domains with `--bank-domain`. Confirm `paused_sources` is roughly the ~15 marketplaces.
4. Apply (A–D) with the same flags plus `--apply`, before the next scheduled search.
5. Observe 24h: search results admitted vs filtered, bank URLs (should be 0 new), run messages, skipped counts
   per run (should drop sharply), new leads and how many have a personal email.

## Rollback
- Exclusions/queries: the JSON `before` block holds the previous `exclusions`, `search_queries`, `auto_search_queries`;
  restore via the campaign form.
- Paused sources: set `active=True` (each has a dated `campaign_tuneup` note). Dismissed bank URLs keep their reason.
- Code: point back at `b2161bc`. No schema change.

## Not in this branch
- F (extraction for the 36 `no_matching_cards` sites) — needs fixtures from those pages; next branch.
- Recipe floor (70 vs 85) — still Lu's decision; changing it pauses all setup jobs.
- crono-jev-soak cron against the old checkout — ops: disable the timer.
- Common Crawl 504s — leave off until the index recovers.
