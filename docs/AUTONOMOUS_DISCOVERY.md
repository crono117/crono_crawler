# Autonomous source discovery

Source acquisition now has its own schedule. A full 500-job crawl, delayed page
requests or a completed run no longer prevent the discovery bot from searching
for new sites. This builds on the lead-yield reporting and queue changes in PR #3.

## Controls and boundaries

- One `SourceSearch` can be queued/processing per campaign. The same existing
  worker executes it; no additional worker or service is installed.
- Search is still opt-in at both server and campaign level. This migration does
  not enable Brave, add a key, increase any budget or start a campaign.
- **Configure source search** on a campaign changes only search controls. It
  preserves the active crawl, its jobs, deferrals, fetch allowance/counters and
  automation policy. The general **Edit campaign** action retains its existing
  pause/cancel behavior.
- Default interval: 60 minutes; configurable from 15 to 1,440. Existing campaign
  and application-wide search quotas remain authoritative and count uncertain
  sends and retries. Search requests use a separate counter from page fetches.
- Up to 10 exact queries remain supported. Optional generated queries combine
  at most six industry phrases, team/contact intents, the first two sales roles
  and the campaign region (at most 30 generated queries). No domains, people or
  contact details are invented. Region is a search hint, not verified geography.
- Eligible queries use existing observed-page-yield sampling; a persistent
  cursor rotates ties. A query that admits a previously unseen origin rests for
  one day. A query with no new origin rests for seven days. Neither outcome proves
  that a site contains usable leads. Failed searches have a one-hour query cooldown
  after their bounded retries finish; provider throttles/backoff also apply.
- Each request returns at most 20 results. `max_new_domains` bounds each independent
  search as well as ordinary crawl runs. A full/old run's domain counter does not
  consume the new search's allowance. Daily search and site-setup quotas still
  bound expansion across searches. There is no search pagination.
- New search results enter the same scoring, exact-scope, source-review and
  automation-policy gates as other discovered URLs. Approved new sites must pass
  scoped probing, recipe validation and a canary. Search snippets are metadata,
  never lead evidence. If the current crawl is full, approved collection work can
  wait for the next run; acquisition and bounded site setup can still proceed.
- Existing run-bound search jobs drain before independent searches begin. Their
  retry dates and budgets are preserved. Missing provider configuration or denied
  API access stops/defer searches without pausing the entire campaign.

## Reversible inventory turnover

Turnover is off by default. When enabled and active inventory is full, a relevant
new team/contact search result can archive up to 20 old URLs per result batch to
make room. Archive selection requires one of:

- An explicit URL/origin dismissal; the dismissal remains in force.
- An unreviewed URL last seen over seven days ago.
- A source with ten distinct successfully processed discovery page URLs and no
  validated contacts. Failures, skipped pages, sitemap jobs and repeat fetches of
  one URL do not establish this condition. This retires unproductive backlog; it
  does not prove the site has no people or that its extraction is correct.

The incoming result must meet the campaign score threshold and direct page-intent
gate. A stronger old candidate is retained unless already dismissed. Active jobs
(including paused runs), manual-review candidates/imports, company follow-ups,
starting URLs, sitemaps, and sources with any stored contact observation are
protected. Missing observations and suppressed/shared-contact leads still protect
their source. No lead, observation, review decision, source approval or policy is
deleted or rewritten.

Archived URLs stay in history and deduplication, but leave active capacity, normal
queueing and policy scans. Repeated search results cannot revive them. Choose the
**Archived** URL filter and **Restore to active inventory** to restore one after
making room. Restore preserves its previous approval/dismissal; it is not a new
authorization and does not dispatch a request itself.

If the inventory is full and no eligible turnover exists, the bot reports that
condition without spending a search request. If results cannot replace eligible
rows, the attempt reports capacity rejections rather than claiming they were saved.

## Desktop rollout (Hermes)

1. Verify the approved revision and test it against an isolated temporary database,
   with external integrations disabled. Do not change the existing production
   checkout in place if it has local work.
2. Stop the existing user service. Back up the actual production SQLite database
   with the SQLite Backup API and verify integrity; preserve private configuration,
   service overrides and local Lab assets outside git. Record the current revision.
3. Point an isolated deployment checkout at the existing production database and
   private configuration. Apply `manage.py migrate`, then `check` and
   `collectstatic --noinput`. Migration 0007 adds search state and archive columns;
   it preserves counters, jobs, approvals, policies and records.
4. Restart through the existing service. Verify exactly one worker, the intended
   checkout/database and healthy loopback HTTP responses.
5. Read the current acquisition status (no writes or network requests):

   ```bash
   .venv/bin/python manage.py discovery_status --campaign 2
   ```

6. To activate broader acquisition, configure the existing private Brave key and
   `BRAVE_SEARCH_ENABLED=1` under the operator's chosen search allowance, then
   restart the existing service. Never print the key. Keep all existing paid-model
   controls and fetch budgets. In **Configure source search**, enable source search,
   generated queries and (if desired) reversible inventory turnover. Existing
   campaign search quotas are preserved unless explicitly edited.
7. Observe a normal worker tick. Compare the new status/attempt report with saved
   candidate rows, new origins and automation jobs. If setup succeeds, use
   `discovery_yield --campaign 2 --days 7` and actual observation links to verify
   leads. A new URL or authorized source is not a new lead.

For the reported Campaign 2 installation, preserve the 5,000 daily fetch allowance,
808+ used requests, eight shared-contact Source 69 leads, recipe threshold 70,
Jev controls and the existing run. Those values are operator/runtime configuration,
not hard-coded defaults for this feature. Search cannot find new domains while
`BRAVE_SEARCH_ENABLED=0`; the UI now says so directly.

Code rollback can leave the additive schema in place. Stop the service first and
preserve newer data. Older code ignores archive/search scheduling state and may
count/revisit archived URLs again; account for that before resuming it. Do not
reverse the migration or restore an older database merely to change code versions.

## Verification

The regression suite uses fictional URLs, mocked search results and isolated test
databases. It covers searches during a full/deferred crawl, independent domain
capacity, policy/setup handoff, query rotation/cooldown, provider gates, quotas,
retry/crash recovery, legacy search coexistence, result accounting, archival
protection/restoration, and staff/CSRF-protected controls. The Lab and campaign
views expose provider readiness, next check, budgets and latest outcomes without
credentials. No live paid search or production lead yield was measured here.
