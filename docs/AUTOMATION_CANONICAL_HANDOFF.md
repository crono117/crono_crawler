# Automatic setup: canonical onboarding handoff

| | |
|---|---|
| Base (live) | `a7dce4086e996a948ca3d42080c37ccb7bd923a6` (capacity fix) |
| Implementation | `d4451dcbd3893edae4ca863be74d7ee4580dc30b` |
| Branch | `fix/automation-canonical-onboarding` |
| Schema | `automation.0005_canonical_onboarding`, additive, reversible |
| Live database | Not read or changed by this work |

## Why

The post-deployment report (September 30, 17:44 UTC) showed that of 25 automatic setup jobs created after the capacity fix, 1 went active, 12 failed and 12 paused. 11 of the failures were `redirect_out_of_scope`, and 14 assessments across the campaign had that outcome. Policy-created sources keep an exact origin (`in_scope`), so a site that redirects `example.com` to `www.example.com` failed before any page was read. `same_site()` already defined the safe equivalence (https upgrade and/or one leading `www.`); it was deliberately not applied to policy-created sources.

Expectation: of the 14 recent jobs that were not redirect failures, 1 reached active. Recovering redirect failures alone should add roughly one active source per ~10 recovered, unless admission and recipe quality also improve. The audit below measures this instead of assuming it.

## What changed

- `automation/canonical.py`: `classify()` a redirect destination against the originally authorized URL (`canonical`, `canonical_path_out_of_scope`, `same_origin`, `downgrade`, `other_subdomain`, `cross_domain`, `invalid`, `unparsed`), and `rebind()`, which runs under row locks and re-verifies generation, state, page, approval kind, policy snapshot, scope hash, rebind cap, denied domains, dismissals and conflicts before changing `Source.url`, bumping the generation and restarting the probe.
- `fetch_probe_page` records URLs the guard refused; on a refused redirect of the depth-0 probe page it calls `rebind()`. `rebound` restarts the probe; `conflict` pauses with the new `canonical_conflict` assessment; anything else keeps the old failure.
- `consider()` treats http/https and www/bare forms as one site for duplicate sources/jobs and origin dismissals (stricter than before).
- `SiteAutomationJob.original_url` and `canonical_rebinds` (migration 0005).
- `manage.py automation_audit` (read-only) and `manage.py automation_requeue_canonical` (dry run unless `--apply`).
- Docs: `docs/AUTOMATION.md`, section "Canonical onboarding".

Unchanged: operator-source scope, collection-time exact scope, robots, public DNS, budgets, delays, the probe/recipe/canary gates, policy values, approvals, Brave, Jev.

## Verification (isolated)

```
manage.py check                              no issues
manage.py makemigrations --check --dry-run   No changes detected
manage.py test                               Ran 632 tests ... OK   (611 before; 21 new)
smoke_local / bulk_import / jev / extraction / inventory   all PASS, exit 0
migrate automation 0004 / 0005               unapply and reapply OK
```

`automation/tests/test_canonical.py` runs the real worker against a local HTTP server with fixture DNS:
- A bare -> www redirect rebinds once, fetches robots.txt for the new origin before the page, reaches active with 2 leads, and the old origin is no longer in scope.
- Cross-domain, other-subdomain and same-site-out-of-path redirects fail closed with no request to the destination.
- A bounce back to the original origin stops after one rebind.
- An existing source on the canonical origin pauses as `canonical_conflict`.
- A dismissed canonical origin is never adopted.
- A canonical host resolving to a private address is still refused before connecting.
- Stale generations, canary pages, the rebind cap, a changed policy and operator sources cannot rebind.
- Same-site duplicate jobs and same-site dismissals block admission.
- Requeue is a no-op without `--apply` and recovers only canonical failures with it.
- The audit classifies jobs correctly, prints no contact values or HTML, and writes nothing.

`classify()` unit cases include lookalikes (`example.com.evil.net`, `wwwexample.com`, `www.www.example.com`, `examp1e.com`, punycode), downgrade, credentials, non-standard ports, non-http schemes, path traversal and parent/child subdomains.

## Rollout

Needs Lu's authorization for anything that changes live state. Steps 1 to 3 are read-only for live.

1. **Build and test isolated.** Fresh clone at `d4451dc` beside the live checkout; bootstrap with a throwaway `DATA_DIR`; run check, drift, full tests and the five smokes. Same procedure as `docs/DISCOVERY_CAPACITY_HANDOFF.md`.
2. **Audit on a copy of the live DB** (backup API copy, `migrate` the copy, never the live file):
   ```bash
   .venv/bin/python manage.py automation_audit --campaign 2 > audit-all.json
   .venv/bin/python manage.py automation_audit --campaign 2 --since 2026-09-30 > audit-post-fix.json
   .venv/bin/python manage.py automation_requeue_canonical --campaign 2 > requeue-dry-run.json
   ```
   Report `summary.redirects`, `by_detail`, `require_page_intent_replay`, `recipe_floor_replay`, `same_site_duplicate_jobs`, and the dry-run eligible list.
3. **Prove recovery on the copy** (optional, spends real requests from the desktop): with the copy and a single temporary worker, `automation_requeue_canonical --campaign 2 --apply --limit 5`, run the worker until those jobs settle, and report how many reached active, paused or failed and why. Do not run this against live without authorization.
4. **Deploy** (authorized separately): same maintenance-window procedure as the capacity handoff (stop the service, back up with the backup API, carry private config, `migrate`, `check`, `collectstatic`, switch the service, restart, verify one worker, correct checkout and DB, and HTTP health). The migration is additive; with no requeue, only new or resumed setups use rebinding.
5. **Recover historical failures** (authorized separately): `automation_requeue_canonical --campaign 2 --apply --limit N`. Requeued jobs run under the current policy snapshot.

### Decision before step 5: recipe floor

The live policy stores `min_recipe_score = 70`; the model validates 85 to 90, and nothing clamps the stored value, so setup accepts recipes below the documented floor. `recipe_floor_replay` shows which active jobs depend on that. Raising it means saving the policy form, which pauses **every** setup job in the campaign, including active ones, until each is resumed. If the floor is going to change, change it before requeueing so recovered sites are judged at the new floor.

## Rollback

Stop the service, point it back at the `a7dce40` checkout, restart, and verify. The additive columns stay; old code ignores them. Sources already rebound keep their canonical URL, which is exactly scoped and valid under the old code. Requeued jobs that are mid-setup simply fail closed on the redirect again under the old code. Do not restore a database backup to undo a deploy.

## Remaining limits

- `robots.txt returned HTML` (3 recent pauses) is unchanged and still fails closed.
- `no_matching_cards` and cards-without-sales outcomes are recipe/content problems this branch does not address.
- Pending URLs at a canonical origin are not re-bound to a rebound source; they stay pending (reported, harmless).
- An https upgrade chain (`http://a` -> `https://a` -> `https://www.a`) uses both rebinds; any longer chain fails closed.
