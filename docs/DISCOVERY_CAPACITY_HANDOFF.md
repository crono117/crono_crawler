# Discovery capacity handoff

| | |
|---|---|
| Base (deployed) | `db911005370833372166b0256c4f8eed5823e2a9` (`origin/feat/autonomous-source-discovery`) |
| Implementation | `117a146178a9c56a37f9d29e4bcce59111c90785` |
| Branch | `fix/discovery-capacity-turnover` (local; not pushed, not deployed) |
| Schema | `discovery.0008_inventory_parking`, additive, reversible |
| Live database | Not read or changed. All numbers below are from tests or a synthetic temporary database. |

## Diagnosis

The worker is healthy. The code shows why a 3,000-URL campaign stops adding useful work:

1. **Approved means in scope, not reviewed.** `register_batch` saves every in-scope link and sitemap entry on an approved source as `approved` and counts it toward `max_candidates`. Most fail the direct-intent half of `ordinary_page_eligible`, so `queue_candidate` never queues them, yet they hold capacity indefinitely. One page contributes up to 2,000 links and one sitemap up to 2,000 entries (entries only need `min_score`). This is the expected source of most of the 2,936 approved rows.
2. **Turnover cannot reclaim them.** `turnover.eligible` archives only operator dismissals, pending rows unseen for 7 days, or rows on sources with 10+ zero-contact pages *and no observations*. Productive sources are fully protected, and turnover runs only during an enabled Brave search with `auto_archive_candidates`. `has_room` is also false whenever inventory is more than 20 over the limit.
3. **The repeating zero-page runs are the collector intake.** `import_collector_backlog` runs every 5 minutes; with no open run it created one, every external link was capacity-rejected, and `finish()` completed it immediately with the "Active URL limit reached" message. Those runs were never crawls, so zero pages is not a worker or extraction failure. The collector links stayed `new` and were retried each pass.
4. **Effect on yield.** Once full, newly found team/profile pages on approved sources are rejected for capacity, which is where validated leads come from.

Confirm (1) on live data with the read-only report below: `active_approved.no_direct_intent` and `parkable_now` should account for most of the inventory.

## Change

- `ranking.low_value_page()` is the intent half of `ordinary_page_eligible`, which now calls it. The same gate drives dispatch, queueing, registration parking and the maintenance scan. Score, keywords, yield and nearby context are not consulted, so parking cannot be caused or reversed by a changing score. Sitemaps and exact starting URLs are exempt, as before.
- Taxonomy listings (`/tag/`, `/tags/`, `/category/`, `/categories/`, `/topic/`, `/topics/`) no longer establish direct intent. This tightens eligibility for every campaign.
- `Campaign.park_low_value_urls` (default **off**; UI: *Park in-scope pages without contact-page intent* on the campaign and source-search forms). When on, new in-scope pages failing the gate are saved `approved`, bound to their source, and archived with `turnover.PARKED_REASON`, outside active capacity. Seeds, sitemaps and company follow-ups are never parked. A parked URL seen again with a direct-intent label returns to active inventory if there is room. Turnover and operator archives still never return automatically.
- `turnover.parkable()` / `park()` scan and archive existing rows in constant queries and 500-row batches. Protected: seeds, operator imports, exact starting URLs, sitemaps, manual review, active jobs, company follow-ups, URLs whose own fetch stored contacts or whose exact page has an observation, and sources in automated setup. Source productivity alone does not protect a page.
- Collector intake opens no run when inventory is full; waiting links stay `new`. Intake-opened runs have `intake_only=True` and an explicit message.
- `DiscoveryRun.capacity_rejected` and `parked` counters; the limit message now includes counts. The run page shows both and an intake-only badge.
- `manage.py discovery_inventory --campaign ID` (read-only JSON). `--park-low-value` and `--enable-parking` report only unless `--apply`.
- `scripts/smoke_inventory.py`: offline smoke at a full pilot's size.

Unchanged: approvals, source bindings, dismissal scopes, robots/public-only/scope checks, source blocking, daily budgets, automation policy, search and Jev gates. No domain is approved automatically; external links stay pending.

## Verification (this branch, isolated)

```
manage.py check                              System check identified no issues (0 silenced).
manage.py makemigrations --check --dry-run   No changes detected
manage.py test discovery                     Ran 207 tests ... OK
manage.py test                               Ran 611 tests ... OK   (585 before; 26 new)
scripts/smoke_local.py                       8 PASS, exit 0
scripts/run_smoke_bulk_import.py             PASS, exit 0
scripts/smoke_jev.py                         PASS, exit 0
scripts/smoke_extraction.py                  PASS, exit 0
scripts/smoke_inventory.py                   4 PASS, exit 0
migrate discovery 0007 / 0008                unapply and reapply OK on a temporary database
```

New tests (`discovery/tests/test_inventory_parking.py`) cover generic terms (sales, contact, partner, about, representative, reseller, brochure, schedule, deck) never qualifying alone, taxonomy, context-only ranking, gate agreement between eligibility and parking, default-off semantics, parking with bindings intact, capacity still enforced for active URLs, seed/sitemap exemptions, unpark rules, tracking-parameter deduplication, parked rows never dispatched, capacity-checked restore, constant-query scan/park, per-row registration cost, all parking protections, intake behavior, and the run page. Existing suites continue to cover robots, redirects, 401/403 blocking, scope and turnover.

**Temporary-database integration** (30-URL limit; 26 approved low-value rows, 2 pending, 1 origin dismissal):
dry run parked nothing; `--apply` parked 26 and active inventory went from 30 to 4; a crawl then reached `/team/` and stored **2 non-synthetic leads** (0 demo); navigation pages were parked, `/blog/` was auto-filtered as before, the external vendor link stayed pending, no Source was created; approvals, bindings, pending rows and the origin dismissal survived parking and rescoring. A crawl with no people completed as `completed` with `needs_recipe_review`, not `failed`.

**Synthetic pilot-scale database** (`smoke_inventory.py`; fictional mix, not live data):

| | Before | After `--park-low-value --enable-parking --apply` |
|---|---|---|
| Active inventory / limit | 3,024 / 3,000 | 376 / 3,000 |
| Decisions (approved / pending / operator-dismissed / auto-filtered) | 2,936 / 75 / 13 / 1,100 | unchanged |
| Archived (parked) | 0 | 2,648 |
| Active approved: queueable / starting / sitemap / no intent | 192 / 48 / 48 / 2,648 | 192 / 48 / 48 / 0 |
| Intake while full | no run opened, 40 links kept `new` | intake run opened, labelled; 25 admitted (new-domain cap), 15 kept |
| Leads (non-synthetic / demo) | 9 / 6 | 9 / 6 |
| Cost | report 1.3 s, 24 queries (4,124 rows) | apply 2.1 s, 63 queries |

Worker lease: unchanged. Parking at registration and intake run inside the existing collector tick under the campaign row lock; the maintenance command takes the same lock and never touches the lease. The report reads the lease (fresh/stale) and changes nothing.

## Rollout (requires explicit authorization)

1. Build an isolated worktree at `117a146` beside `crono_crawler-deploy-db911005`; do not modify the deployed checkout. Run `python3 setup.py --no-user`, the test suite and smokes there with no production `DATA_DIR`.
2. Diagnose on a copy, never the live file (the report needs migration 0008, so it cannot run against the unmigrated live database):
   ```bash
   mkdir -p /tmp/inv-copy
   sqlite3 /home/crono/dev/crono_crawler/data/leads.sqlite3 ".backup '/tmp/inv-copy/leads.sqlite3'"
   sqlite3 /tmp/inv-copy/leads.sqlite3 "PRAGMA integrity_check;"
   export DATA_DIR=/tmp/inv-copy DATABASE_URL= ACTIVITY_LOG=off BRAVE_SEARCH_ENABLED=0 JEV_MODE=off
   .venv/bin/python manage.py migrate
   .venv/bin/python manage.py discovery_inventory --campaign <id>
   .venv/bin/python manage.py discovery_inventory --campaign <id> --park-low-value --enable-parking --apply
   ```
   Review `active_approved`, `parkable_top_origins`, `active_fetch_state` and `runs.intake_only`. Spot-check parked URLs from the top origins before going further.
3. Deploy only if the copy shows parkable rows are genuinely non-person pages: stop the service; back up the live database with the SQLite backup API and verify integrity; keep `.env` and service overrides; point the new checkout at the existing database; `migrate`, `check`, `collectstatic --noinput`; restart; confirm exactly one worker, the intended checkout and database, and healthy loopback responses.
4. Run the same report and, if authorized, the same `--apply` against live. Then watch run messages, `capacity_rejected`/`parked` on run pages, and `discovery_yield --campaign <id> --days 7` for real leads. A freed slot or new URL is not a new lead.

Conditions before deploying: operator authorization; a verified live backup; copy results reviewed; one worker; no change to budgets, search, Jev or campaign policy beyond the explicit parking flag.

## Rollback

- **Behavior only:** clear *Park in-scope pages* on the campaign. New pages count toward capacity again; existing parked rows stay archived and restorable.
- **Un-park rows:** individually via the Archived filter's capacity-checked restore; in bulk only after making room (for example raising `max_candidates` deliberately), since restoring all parked rows refills the limit.
- **Code:** stop the service, redeploy `db911005`. The additive columns can stay; older code ignores `park_low_value_urls`, `intake_only` and the counters, and treats parked rows like any archived row (never queued, not counted). If schema removal is required, `manage.py migrate discovery 0007` on the new code first, then switch code. Do not restore an older database backup just to change code; it discards leads collected since.

## Remaining blockers and limits

- Live composition is inferred from code; the copy report is the confirmation step.
- Parking frees capacity; it does not create leads. Yield still depends on sources with working recipes, the paused 401/403/robots/challenge sources, and new-domain setup.
- `has_room` still reports full when inventory is more than 20 over the limit (bounded turnover by design). Parking normally clears this.
- A parked page later linked with a better label is recovered only if that link is seen again; restore is otherwise manual.
- www/trailing-slash duplicates are reported, not merged.
- No live crawl, paid search or Jev request was run.
