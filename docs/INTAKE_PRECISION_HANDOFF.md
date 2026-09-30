# Intake precision handoff

| | |
|---|---|
| Base (live) | `e48eedf2075bfe0fcb8409b19d9dd40656e676f1` (canonical onboarding) |
| Implementation | `c481d4bc549d727688c87ec9b2a18316281879af` |
| Branch | `fix/intake-precision` |
| Schema | `discovery.0009_origin_share`, additive, reversible |
| Live database | Not read or changed by this work |

## Problem (Hermes diagnosis after the canonical deploy)

1. **Daily setup slots are spent.** 25 of 25 automatic jobs were created today, and 0 are runnable. `require_page_intent=false` let generic homepages take every slot. The copy audit found all 25 would have been rejected with intent required.
2. **Active inventory is back at 3,000/3,000.** The cause was not identified.
3. **12 canonical-recoverable failures** are still failed. The requeue was not authorized.
4. **Funnel:** 45 jobs, 2 active, 23 paused, 20 failed; 17 had no people page.

Two constraints made the obvious fixes costly:

- **Any policy save paused every setup job,** including active ones, so flipping `require_page_intent` would have stopped the only working sources.
- **The form can't save the stored policy as it stands.** It holds `min_recipe_score=70`, below the form's 85–90 bounds, so any save through the form must also change a setup field.

## Change

- **Admission vs setup.** Authorization compares only setup-bounding fields (`policy.SETUP_FIELDS`). Admission fields (`min_url_score`, `max_sites_per_day`, `require_page_intent`) no longer revoke admitted jobs. The form pauses jobs only if a setup field or `enabled` changed. `manage.py automation_policy` changes admission fields alone (bounded, dry run unless `--apply`).
- **External parking.** With `park_low_value_urls` on and an enabled policy requiring intent, new pending external pages without direct intent are parked (reason: external parking). Existing ones can be parked with `discovery_inventory --park-low-value --apply`. A parked page comes back if it is rediscovered with a direct-intent label and there is room.
- **Per-origin share.** `Campaign.max_active_per_origin` defaults to 0, which means unlimited. When set, new pages from an origin at its share are parked. `discovery_inventory --origin-cap N` parks each origin's lowest-scoring unprotected excess (dry run unless `--apply`).
- **Diagnostics.**
  - `discovery_inventory` adds `active_by_origin_top`, `last_24h` and `active_pending`.
  - `automation_audit` adds `pending_pool`: pending pages and distinct sites that could enter setup with intent optional, and with intent required at 70, 50 and 35.

Unchanged: approvals, source bindings, scope, robots, the recipe/canary gates, `min_recipe_score`, budgets, Brave and Jev. Every new behavior is off until an operator sets it.

## Verification

```
manage.py check                              no issues
manage.py makemigrations --check --dry-run   No changes detected
manage.py test                               Ran 643 tests ... OK   (632 before; 11 new)
five offline smokes                          all PASS, exit 0
migrate discovery 0008 / 0009                unapply and reapply OK
```

New tests cover:
- **Admission changes:** they leave `authorized()` true, a setup change revokes it, and the form pauses only on setup changes.
- **The policy command:** it is dry-run first, enforces bounds, and leaves a stored 70 recipe floor untouched without pausing jobs.
- **External parking:** it happens only under an enabled intent policy, and a parked page comes back on rediscovery with intent.
- **Origin share:** it parks the excess, exempts seeds, adds no per-row queries, and 0 means unlimited.
- **Excess parking:** it keeps the highest scores and never touches protected rows.
- **Reports and audit:** the report composition fields and `pending_pool` counts are covered.

## Operator sequence (each step gated on the previous one's data)

Commands run from the deployed checkout, for campaign 2.

1. **Deploy.** Same procedure as `docs/DISCOVERY_CAPACITY_HANDOFF.md` (backup, migrate, check, collectstatic, switch, verify one worker). No behavior changes until the next steps.
2. **Read-only diagnosis:**
   ```bash
   manage.py discovery_inventory --campaign 2 > inv.json
   manage.py automation_audit --campaign 2 --no-jobs > audit.json
   ```
   Find what refilled the limit in `last_24h` (by decision, method, origin) and `active_by_origin_top`. Size the intent change from `pending_pool`.
3. **Recover canonical failures.** This is the only setup work available today, because requeue creates no job and uses no daily slot.
   ```bash
   manage.py automation_requeue_canonical --campaign 2 --limit 12 --apply
   ```
4. **Require page intent**, only if `pending_pool` shows sites admissible with intent required. Pick the lowest `--min-url-score` among 70/50/35 that still leaves a reasonable number of sites; if none qualify at any threshold, stop and report instead.
   ```bash
   manage.py automation_policy --campaign 2 --require-page-intent on --min-url-score <70|50|35> --apply
   manage.py discovery_inventory --campaign 2 --park-low-value --apply   # parks no-intent pending externals
   ```
   Existing jobs and the two active sources keep running.
5. **Origin share**, only if one or a few origins dominate `active_by_origin_top` or `last_24h.counted_top_origins`:
   ```bash
   manage.py discovery_inventory --campaign 2 --origin-cap 150            # dry run: would_park_origin_excess
   manage.py discovery_inventory --campaign 2 --origin-cap 150 --apply
   ```
6. **Observe for 24 hours (one UTC day).** Record new jobs and their outcomes, rebinds, capacity rejections, parked counts by reason, and new non-synthetic leads.

The recipe floor (`min_recipe_score` 70 vs 85) is still a separate decision. Changing it goes through the form and pauses every job.

## Rollback

- **Settings:** `automation_policy --require-page-intent off --apply`; `discovery_inventory --origin-cap 0 --apply` (stops new origin-share parking; already parked rows stay archived and restorable).
- **Code:** stop the service, point it at the `e48eedf` checkout, restart. The additive column stays and older code ignores it. Parked rows stay archived, uncounted and never queued under older code.
