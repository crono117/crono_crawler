# Discovery pilot

Trial branch: `feat/discovery-v1` in `crono117/crono_crawler`.

Discovery is part of the same Django application and runs in the existing worker. No additional service, model, browser installation, database server, or API key is needed for link and sitemap discovery. Existing contacts, accounts, source settings, reviews and queues are retained by the additive migration.

**Automatic onboarding is now available:** enable a campaign policy to authorize qualifying new sites without per-domain approval. Sites must still pass scoped probing, recipe validation and a canary before normal collection. See [the automation guide and current Hermes upgrade instructions](AUTOMATION.md). The manual workflow below remains available when a policy is disabled or a URL needs an exception.

For the real Host Merchant zero-contact result, see the [controlled follow-up and Hermes handoff](HOST_MERCHANT_PILOT.md). The follow-up adds precise exclusion rules, an optional contact-discovery filter preset, local CSS recipe diagnostics and dashboard health warnings. It does not install an unverified site recipe or change existing campaign settings automatically.

## Upgrade an existing local installation

These steps are suitable for Hermes on the home desktop. Use the existing checkout and its virtual environment. Preserve any local code changes; do not reset or overwrite them to switch branches. Inspect `git status` first and use a separate checkout if local development would conflict.

1. Stop the existing web and worker processes using the service/launcher that currently manages them. Keep only one worker per database.
2. Back up the existing database and private configuration outside the repository. For the default SQLite installation, after stopping both processes, copy the entire `data` directory (including any WAL/SHM files) and `.env` into a private backup directory. If using a custom `DATA_DIR` or PostgreSQL, back up that actual database instead. Do not commit backups. Keep a note of the previously deployed commit.
3. From the existing repository directory, fetch and switch to the trial branch:

   ```bash
   git fetch origin
   git switch feat/discovery-v1
   python3 setup.py --no-user
   .venv/bin/python manage.py check
   .venv/bin/python manage.py test
   .venv/bin/python scripts/smoke_local.py
   ```

   Git should create the local tracking branch from `origin/feat/discovery-v1` if it does not exist locally. If the branch already exists, inspect its state and use a normal fast-forward update. The bootstrap preserves an existing `.env` and applies migrations; it does not recreate accounts. The smoke script uses a separate temporary database and does not modify your working contacts or credentials.

4. Queue the optional offline demo:

   ```bash
   .venv/bin/python manage.py init_discovery_demo
   ```

5. Restart the existing web/worker service, or run `python3 run-local.py`. Open http://127.0.0.1:8000 and choose **Discovery**. There is no second worker to install.

On Windows, use a supported interpreter through `py` and replace `.venv/bin/python` with `.venv\Scripts\python.exe`. Linux is the platform verified in this build.

If you used a separate checkout, do not silently start it with an empty database. Configure it to use a deliberate copy of your local database/configuration for testing, or explicitly switch the existing service to the new checkout after backup. Keep the original installation stopped if both would access the same database.

For rollback, stop both processes and follow [the automation rollback procedure](AUTOMATION.md#hermes-upgrade-handoff) before switching to older code: earlier versions do not understand setup gates, so automatic sources must be disabled first. Restoring a pre-upgrade database backup also rolls back any contacts collected since that backup, so do that only deliberately.

## Offline acceptance check

The **Offline discovery demo** follows a fictional directory to team pages, reads a sitemap to find a page absent from the directory links, and collects three fictional people: **Dana Discovery**, **Riley Example**, and **Casey Sample**. Their addresses use `example.com`.

An exact URL, `https://new-vendor.example.org/reps/`, appears as **Needs review**. It is never fetched. All demo responses come from fixed packaged fixtures; no network requests are made. If you already ran the original demo, your database should contain its three people plus these three. Repeating the discovery demo refreshes its records without adding duplicates.

Check the campaign page, run details, scored URL list, source evidence, pause/resume, and dismiss controls. Pause the demo campaign when finished testing.

## Set up a real campaign

1. Add/review a starting source through **Sources**. A public industry directory or company team page is useful. Configure its exact origin, approved path scope, source business category, request delay and any required CSS recipe.
2. In **Discovery → New campaign**, select one or more approved sources. Configure industry keywords, sales-role phrases and exclusions, one phrase per line. The region field adds a ranking hint; it does not verify geography.
3. Keep search off initially. Start with 5–10 page/sitemap jobs per run, a small depth, and a bounded daily fetch allowance. Save, then **Start / resume**. Inspect extraction yield before increasing the budget.
4. Review run results and actual evidence in **Lead database**. A successful page with zero validated contacts is reported separately from a failed fetch/parser/extractor. Zero contacts can mean a CSS recipe is needed; it does not prove the site has no useful people.
5. Enable **Automation policy** if qualifying new domains should set themselves up. For exceptions or the manual mode, open **Review discovered URLs**. Each URL has a priority score, explanation, last discovery context, provenance, and job history. Attach a reviewed source whose origin/path scope covers it, or choose **Configure a new source**, complete the source review, and save it as approved.

Approval attaches that source to the campaign and records permission for its exact origin/path scope. Permission is separate from page eligibility: an approved ordinary page is queued only when its current campaign score meets the page threshold and its exact URL path or link label has direct contact/person-page intent. An approved URL that is not eligible remains approved and retained in history; it is simply not queued. Explicit reviewed starting URLs and in-scope sitemap jobs are exempt from the ordinary page-eligibility gate. Dismissed URLs stay dismissed until explicitly approved individually. You can dismiss all URLs for one exact origin within a campaign.

Campaigns use their own start/pause and page/depth limits; a source's regular collection schedule can stay off to avoid duplicate fetching. Source origin/path restrictions, robots policy and delays apply to both workflows. Pausing or editing an operator-configured source pauses its active campaigns. Automatic source failures pause that site's setup without stopping other campaign sources. Editing a campaign cancels its old unfinished run and leaves it paused. Legacy in-flight pages may finish saving; automatic setup rechecks its authorization and generation before committing results.

When a bounded batch competes for capacity, exact URLs without a prior page snapshot are considered before previously fetched URLs. Within each tier, discovery rotates one item per exact origin per pass, then spills over until the batch is exhausted; a single origin can still use all remaining capacity. Priority and stable discovery order are preserved within each origin. This changes ordering only: it does not deduplicate results, cap an origin, change scores/reasons or approvals, widen source scope, or authorize a search/link result for fetching.

At the start of a new run, up to half the page/sitemap allowance (rounded up) is reserved for already-approved, never-fetched non-seed pages remembered from earlier discovery. When that start was given a fresh index batch, those URLs claim the reservation before older remembered pages. The reservation uses the same origin rotation and all ordinary queueing gates; an ineligible candidate consumes no slot. Remaining capacity goes to starting-page refreshes, then seed sitemaps, then other remembered URLs. This prevents repeated seeds and sitemaps from filling the run before known profiles can be fetched, while retaining refresh capacity. An unused reservation flows back to the other work. Resuming an existing run preserves its queue and deferrals. An eligible URL that does not fit is left approved with a deferred last result; it is not given a job.

## Ranking and boundaries

- Direct contact/person-page terms in the exact URL path or link label receive +35. Strong signals include team, staff, people, profile, bio/biography, leadership, representative/rep pages, and explicit combinations such as sales team, sales representatives, and sales agents. Bare `sales` is not page intent. Sales sheets, decks, playbooks, brochures/product collateral, scheduling pages, social pages, and document-sharing links do not qualify for the page-type bonus. Campaign industry phrases add +25, sales-role phrases +20, and a region phrase +10; these remain priority-only signals and cannot make a generic page eligible. Nearby context can support those priority scores but never establishes direct page intent. Navigation/header/footer links use their own labels as context. Editorial/legal/recruitment paths or labels reduce priority by 20; product/device/software paths or labels reduce it by 25. Sources with human-reviewed contacts can receive a +10 preference when URLs are discovered. Scores are priority signals, not confidence percentages. An enabled policy requires both its score threshold and direct intent before authorizing a bounded probe; recipe release has separate evidence gates.
- External links the regular collector finds on a campaign's approved sources are handed to that campaign. While a run is open they are registered immediately; each new run also imports unreviewed links from the last `DISCOVERY_YIELD_WINDOW_DAYS` (up to 200). A lightweight intake pass also runs independently every 5 minutes (`Campaign.next_intake_at`, `discovery.services.intake_due`/`import_collector_backlog`) so a fresh collector link doesn't sit unqueued for a whole campaign cycle; it opens or reuses a run just for the import, without reseeding sources/sitemaps. Each such link is marked handled only once its actual registration outcome is known, so one the campaign has to skip for capacity reasons is retried later rather than lost. Within one registration batch, higher-scored candidates are given first claim on the day's automatic-authorization budget, regardless of arrival order. They use method `collector` and the ordinary registration path, so exclusions, scoring, pending review or policy authorization for new domains, and every queueing gate apply unchanged. Nothing is fetched by the hand-off, and a hand-off failure is logged without affecting the saved collection page. The regular review list is unchanged.
- Observed yield adjusts priority per exact origin. Over a rolling window (`DISCOVERY_YIELD_WINDOW_DAYS`, default 60, UTC run creation time), discovery counts distinct exact page URLs whose page job completed (`done`) and how many of them stored at least one validated contact. Counts are shared across campaigns because they describe the site. An origin with 3 or more completed pages and no validated contacts receives -15; an origin with at least 2 productive pages and a smoothed rate `(1 + productive) / (2 + fetched)` of at least 0.5 receives +10. Released setup-canary pages also count, so a newly onboarded site starts with the evidence its canary produced; each exact URL counts once across canary and discovery fetches. Failed, skipped and sitemap jobs, and failed canaries, are not counted. The adjustment is shown as a reason, is clamped so it never makes a score negative (it cannot exclude or dismiss a URL), never establishes direct contact-page intent, and never overrides exclusions, approvals, scope or the explicit-start floor. The stored score keeps a negative adjustment, so the page sorts later. A direct contact page whose rank already met the campaign minimum before that adjustment stays eligible; a homepage, contact form, exclusion, or editorial rank that is already under the minimum does not. The penalty recovers automatically once the site yields a validated contact or the empty history leaves the window. Zero contacts often means a missing CSS recipe rather than an empty site, so review the recipe before dismissing a low-yield origin.
- Offline evaluation: `manage.py discovery_eval --campaign ID --export labels.json [--limit 150]` writes a template of recent page candidates with blank labels. Set each to `reps`, `team_no_reps` or `irrelevant` (blank entries are skipped) and list expected site origins under `sites`. Then `manage.py discovery_eval --campaign ID --labels labels.json [--json]` reports ranker average precision and precision@10/50, eligibility-gate precision and recall (with rep pages the gate rejects and non-rep pages it passes), and site recall by discovery method. It uses the live scoring, including yield history, and makes no network requests. See `docs/eval/discovery_labels.example.json`.
- Optional Common Crawl lookups (`COMMON_CRAWL_ENABLED=1`, off by default): `manage.py discovery_commoncrawl --campaign ID [--collection CC-MAIN-YYYY-WW] [--dry-run]` queries the public Common Crawl CDX index (newest collection unless one is given) for archived HTML pages on up to 25 approved campaign sources, one exact host each, with a one-second pause between lookups. Only the index is contacted, never the target sites. Archived `http` URLs map to the source's `https` origin; only exact in-scope URLs with direct contact-page intent are offered, and they register into the open run with method `commoncrawl` through the ordinary path, so exclusions, scoring and queueing gates apply and the later page fetch follows normal robots, scope and budget rules. Common Crawl data is CC BY 4.0; keep attribution.
- Source searches have a separate schedule and persistent queue, so full or deferred crawl runs do not block new sources. Eligible queries are chosen using observed page yield, with a rotating tie-break order. New-origin queries rest for one day; duplicate-only, filtered or empty searches rest for seven days. Generated queries are opt-in. See [autonomous discovery](AUTONOMOUS_DISCOVERY.md) for controls and deployment checks.
- `manage.py discovery_yield [--campaign ID] [--days N] [--limit N]` first reports new production lead records across setup canaries, discovery and regular collection, counted once per stored identity by `Lead.first_seen`. A record must have at least one non-demo source observation; demo-only records are excluded. Counts include every review state, with shared-company-contact records called out separately. A refresh does not make an existing record new. With `--campaign`, attribution uses current source membership and totals across campaigns may overlap. The discovery-only section reports harvest rate (pages with validated contacts / completed pages) by discovery method, search query and origin, including origins that produced nothing. Its new-contact counters still come from `DiscoveryJob.new_contacts` and cannot represent setup or regular collection. Released setup canaries have their own page-yield section; demo jobs are excluded from both job sections. Method and query reflect each URL's latest discovery context. The command is read-only and makes no network requests.
- Exclusions accept plain phrases, `path:blog` for path-only matching, and `domain:x.com` for exact-host/subdomain matching. Simple plural and hyphen/space variants are supported. The optional contact-discovery preset appends common noise filters. Saving a campaign recalculates existing URL scores while preserving review decisions, cancels the previous unfinished run and leaves the campaign paused. Set page priority around 35 after reviewing filters and the resulting URL list. Automatic new-site setup has a separate policy threshold, default 70, and additional scope/domain/budget checks.
- Exact page paths and meaningful query parameters are retained. Tracking parameters/fragments are removed, while obvious login, calendar, filter, session and asset URLs are excluded. Limits stop cyclic links and unbounded expansion.
- New domains are recorded from links/search metadata. They can be authorized by an operator or an enabled campaign policy and attached to the campaign. Policy authorization creates a paused Source for bounded setup, not immediate normal crawling (when its canary passes, its approved URLs join the open run through the ordinary gates), and it cannot authorize generic collateral without direct contact-page intent regardless of contextual score. DNS/public-network guards, source authorization, the current page threshold, and direct intent are checked again immediately before an ordinary queued page is dispatched, so a stale job cannot bypass changed campaign eligibility. No policy may silently widen an existing source's paths.
- Sitemap URL sets, nested indexes, and gzip files are supported. Compressed and decompressed bodies are capped at 2 MiB, parsing disables DTD/entities/external references, each map considers at most 2,000 entries, and nested indexes are limited to depth 3. Sitemap and page URLs must remain in approved scope. A `/team`-only source does not authorize fetching `/sitemap.xml`; robots-listed maps outside scope are ignored.
- Discovery uses HTML fetches. JavaScript-only pages may need the existing browser collector and a source recipe separately. No model is installed or enabled by this feature; existing extraction settings still apply.
- Source business categories and person/page service evidence retain their existing meanings. Campaign relevance never becomes a person's service tag. Search snippets never create contact records.
- Daily fetch/search counters survive restarts and pause/resume, using UTC days. A fetch attempt includes a robots check or a page/sitemap request; redirect hops are additionally bounded by the transport. Retries count. A crash can conservatively consume an attempt even when no response was saved. Offline demo pages do not consume network quotas.
- Limits apply per campaign; regular source runs have their own limits. Search also has a shared application-wide daily cap. One worker alternates discovery and normal collection, and rotates between runnable campaigns. It uses the existing per-origin throttle and crash-recovery lease.
- Low-value parking (`Campaign.park_low_value_urls`, off by default). Registration marks every in-scope URL on an approved source `approved`, because approval records scope permission, and each such row counted toward `max_candidates` even when the page gate could never queue it. Navigation links and sitemap entries therefore filled the limit, after which new team/profile pages were rejected for capacity. With parking on, a newly found in-scope page without direct contact-page intent (`ranking.low_value_page`, the same intent check `ordinary_page_eligible` uses; score, keywords, yield and nearby context are not consulted) is saved approved and bound to its source but archived with the parked reason, outside active capacity. Seeds, exact starting URLs, sitemaps and company follow-ups are never parked. A parked URL seen again with a direct-intent label returns to active inventory when there is room; turnover and operator archives still never return automatically. Old semantics: every approved row counts. New semantics (opt-in): approved rows the gate can never queue are parked. Taxonomy listings (`/tag/`, `/category/`, `/topic/` and plurals) no longer establish direct intent in any mode.
- `manage.py discovery_inventory --campaign ID` reports, read-only, what occupies active inventory (review decisions, inventory state, approved rows by gate outcome, fetch state, www/trailing-slash duplicates, parkable and turnover-eligible counts, recent runs including intake-only ones, job statuses, non-synthetic vs demo leads, worker lease, database and checkout). `--park-low-value` and `--enable-parking` are dry runs unless `--apply`; applying archives existing parkable rows in bounded batches and changes no decision, source, dismissal, job, snapshot or lead. Protected from parking: active jobs, URLs whose own fetch stored contacts or whose exact page has a contact observation, manual review/imports, and sources in automated setup. Source productivity alone does not protect a page.
- The five-minute collector intake no longer opens a run when active inventory is full; waiting collector links stay `new` and are offered again once there is room. Runs opened by intake are marked `intake_only` and say so in their message, so a zero-page intake run is not mistaken for a worker or extraction failure. Runs record `capacity_rejected` and `parked` counts.
- External pages are parked too, but only when parking is on **and** an enabled site policy requires direct page intent: automation can then never admit a pending external page without intent, so it would only hold capacity until it went stale. It is saved `pending`, unbound, archived with the external-parking reason, and returns to active review when rediscovered with a direct-intent label and there is room. With intent optional or no enabled policy, external pages stay in operator review as before.
- Per-origin share (`Campaign.max_active_per_origin`, 0 = unlimited, the default). When set, a new page from an origin that already holds that many counted URLs is parked with the origin-share reason instead of taking active capacity, so one large site (for example an SEO directory whose location pages all carry role wording) cannot fill the inventory. Seeds, sitemaps and company follow-ups are exempt. `discovery_inventory --origin-cap N` reports, and with `--apply` sets the share and parks each origin's lowest-scoring unprotected excess (seeds, sitemaps, imports, manual review, start URLs, active or productive jobs, contact evidence and setup sources are never selected). Origin-share parking does not reverse automatically; restore rows individually or raise the share.
- `discovery_inventory` also reports `active_by_origin_top`, `last_24h` (rows created, still counted, by decision, method and origin, and how many were parked) and `active_pending`, which show what refilled the limit.
- Saved URL caps do not delete history. Automatically filtered and archived URLs do not consume active inventory capacity. Optional source-search turnover archives up to 20 eligible old URLs per result batch only when a relevant new result needs room. Explicit dismissals survive archiving; archived URLs never reactivate automatically. The Archived filter provides a capacity-checked restore action. Starting pages, active jobs, manual imports and sources with contact observations are protected. Only the latest discovery context/referrer is retained per URL; this is not a complete historical web graph.

Ranked intake and search precision (opt-in, off by default): `SitePolicy.explore_share` reserves daily setup slots for direct team/person pages and ranks the rest by tier, yield and past setup outcomes; `Campaign.search_precision` makes generated Brave queries target team/people page titles. See [the intake ranking handoff](INTAKE_RANKING_HANDOFF.md) before enabling either, and `manage.py automation_intake` to preview.

## Optional Brave web search

Link/sitemap discovery expands from starting sources. Search can find sites that are not linked from them. Enable only if you have an appropriate Brave Search API account/key. Usage is subject to the provider's plan and terms.

Add these settings to your private environment/configuration and restart the app:

```dotenv
BRAVE_SEARCH_ENABLED=1
BRAVE_SEARCH_API_KEY=your-private-key
BRAVE_DAILY_SEARCH_LIMIT=20
```

Then open the campaign’s **Configure source search** page. Enable search and enter exact queries, or opt into generated queries. Saving these controls preserves existing crawl jobs, fetch counters, policies and lead reviews. Exact query examples:

```text
"merchant services" "sales representatives" California
"POS reseller" "our team"
```

Each independent search performs one result page (up to 20 results), at most once per configured interval (default 60 minutes), subject to persisted retries and daily quotas. At most one search per campaign is pending. Up to 10 exact queries can be combined with at most 30 generated queries from six industry phrases, team/contact intents, the first two sales roles and the campaign region (with `search_precision`, `intitle:` team/staff/people/agents/representatives queries instead of the generic intents). Exact queries are never rewritten. There is no paginated search. Search-only campaigns are supported. Existing run-bound search jobs drain first with their original delays; new searches use the independent queue.

The adapter uses the [Brave Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get), not its AI answer endpoint. It fixes the destination, disables redirects for API-key requests, bounds responses, and handles rate limits with persisted backoff. Keys stay in the environment and never appear in the dashboard. Both the campaign and shared daily cap must permit a request; failed attempts also count. This is a request cap, not a dollar-based billing limit.

The optional search adapter has deterministic contract tests. No live paid API request or real lead-site crawl was performed in this workspace; validate a small real campaign on your desktop before relying on its yield.

## Page-chrome filter

Outbound links that are page chrome rather than candidate sources — social profiles and share buttons, document/file shares, scheduling pages, link shorteners, app-store listings, review/event platforms, login/app/docs/CDN subdomains, signup/application forms, legal pages, feeds/sitemaps/`cdn-cgi` helpers, help/sponsorship pages, and malformed URLs — are recorded as automatically dismissed with `Filtered automatically: <reason>` in their reasons (collector links as dismissed `SourceCandidate`s). They are kept for analysis but never enter the review queue. Single-label hostnames (e.g. a broken `href="http://saas"`) are rejected by `clean_url`. `manage.py refilter_discovery` applies the current rules to already-queued items (dry run unless `--apply`; no network requests) and withdraws a policy approval that was granted to a URL now considered invalid; operator approvals are left alone.

The Crawler lab's counter splits waiting items into "needs your review" (positive score, or a team/about/contact page or label), low-signal, and auto-filtered.

## Access blocks

A 401/403, a bot challenge page, or robots.txt served as HTML (or disallowing the page) stops that
source only: `Source.blocked_at`/`blocked_reason` record when and why, its queued collector and
discovery work is closed without requests, and later runs do not seed it again. Other sources and the
campaign keep running, and nothing retries the blocked source automatically. An operator's "Run now"
on the source is the explicit decision to try again and clears the record. Policy-created sources
additionally pause their setup job, which records a `blocked` site assessment.

## Scheduled Common Crawl lookup

`discovery_commoncrawl` retries a busy index answer once per host and then skips that host. The
summary says `skipped after one retry` and does not schedule a later index attempt. Blocked sources
are not looked up. `--start-if-idle` starts the active campaign's ordinary run when none is open and
gives that run the index batch before remembered pages take its page slots. If a run is already
open, the same batch is appended. The command prints discovered, newly registered, approved, queued
and deferred counts separately. Fetched and lead-producing counts are recorded later by the collector.
Registration holds the campaign row lock and does not take the collector lease or fetch target sites.
An example weekly systemd user timer runs
`manage.py discovery_commoncrawl --campaign <id> --start-if-idle` with `COMMON_CRAWL_ENABLED=1` set
only in that unit's environment. Only index.commoncrawl.org is queried; target sites are contacted
later, if at all, through the ordinary discovery gates, robots checks and budgets.
