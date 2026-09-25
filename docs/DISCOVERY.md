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

## Ranking and boundaries

- Direct contact/person-page terms in the exact URL path or link label receive +35. Strong signals include team, staff, people, profile, bio/biography, leadership, representative/rep pages, and explicit combinations such as sales team, sales representatives, and sales agents. Bare `sales` is not page intent. Sales sheets, decks, playbooks, brochures/product collateral, scheduling pages, social pages, and document-sharing links do not qualify for the page-type bonus. Campaign industry phrases add +25, sales-role phrases +20, and a region phrase +10; these remain priority-only signals and cannot make a generic page eligible. Nearby context can support those priority scores but never establishes direct page intent. Navigation/header/footer links use their own labels as context. Editorial/legal/recruitment paths or labels reduce priority by 20; product/device/software paths or labels reduce it by 25. Sources with human-reviewed contacts can receive a +10 preference when URLs are discovered. Scores are priority signals, not confidence percentages. An enabled policy requires both its score threshold and direct intent before authorizing a bounded probe; recipe release has separate evidence gates.
- External links the regular collector finds on a campaign's approved sources are handed to that campaign. While a run is open they are registered immediately; each new run also imports unreviewed links from the last `DISCOVERY_YIELD_WINDOW_DAYS` (up to 200). They use method `collector` and the ordinary registration path, so exclusions, scoring, pending review or policy authorization for new domains, and every queueing gate apply unchanged. Nothing is fetched by the hand-off, and a hand-off failure is logged without affecting the saved collection page. The regular review list is unchanged.
- Observed yield adjusts priority per exact origin. Over a rolling window (`DISCOVERY_YIELD_WINDOW_DAYS`, default 60, UTC run creation time), discovery counts distinct exact page URLs whose page job completed (`done`) and how many of them stored at least one validated contact. Counts are shared across campaigns because they describe the site. An origin with 3 or more completed pages and no validated contacts receives -15; an origin with at least 2 productive pages and a smoothed rate `(1 + productive) / (2 + fetched)` of at least 0.5 receives +10. Released setup-canary pages also count, so a newly onboarded site starts with the evidence its canary produced; each exact URL counts once across canary and discovery fetches. Failed, skipped and sitemap jobs, and failed canaries, are not counted. The adjustment is shown as a reason, is clamped so it never makes a score negative (it cannot exclude or dismiss a URL), never establishes direct contact-page intent, and never overrides exclusions, approvals, scope or the explicit-start floor. A penalty can hold a URL below a high page threshold; it recovers automatically once the site yields a validated contact or the empty history leaves the window. Zero contacts often means a missing CSS recipe rather than an empty site, so review the recipe before dismissing a low-yield origin.
- Offline evaluation: `manage.py discovery_eval --campaign ID --export labels.json [--limit 150]` writes a template of recent page candidates with blank labels. Set each to `reps`, `team_no_reps` or `irrelevant` (blank entries are skipped) and list expected site origins under `sites`. Then `manage.py discovery_eval --campaign ID --labels labels.json [--json]` reports ranker average precision and precision@10/50, eligibility-gate precision and recall (with rep pages the gate rejects and non-rep pages it passes), and site recall by discovery method. It uses the live scoring, including yield history, and makes no network requests. See `docs/eval/discovery_labels.example.json`.
- Optional Common Crawl lookups (`COMMON_CRAWL_ENABLED=1`, off by default): `manage.py discovery_commoncrawl --campaign ID [--collection CC-MAIN-YYYY-WW] [--dry-run]` queries the public Common Crawl CDX index (newest collection unless one is given) for archived HTML pages on up to 25 approved campaign sources, one exact host each, with a one-second pause between lookups. Only the index is contacted, never the target sites. Archived `http` URLs map to the source's `https` origin; only exact in-scope URLs with direct contact-page intent are offered, and they register into the open run with method `commoncrawl` through the ordinary path, so exclusions, scoring and queueing gates apply and the later page fetch follows normal robots, scope and budget rules. Common Crawl data is CC BY 4.0; keep attribution.
- Search queries are ordered by observed yield. When a run starts, each configured query's history (distinct fetched result pages and how many stored validated contacts, credited to each URL's latest discovering query, inside the yield window) feeds a Thompson sample from `Beta(1 + productive, 1 + empty)`, mapped to search-job priority 80–90. Proven queries usually spend the daily search budget first; untried queries keep a uniform prior and are still explored; results never fetched (for example new domains awaiting review) add no evidence. The queued search job's message shows the history and sampled priority. Search budgets, rate limits and result handling are unchanged.
- `manage.py discovery_yield [--campaign ID] [--days N] [--limit N]` prints a read-only harvest-rate report (pages with validated contacts / completed pages) by discovery method, search query and origin, including origins that produced nothing, plus the number of new (previously unknown) contacts per group from `DiscoveryJob.new_contacts`, and a separate section for released setup canaries. Method and query reflect each URL's latest discovery context. It makes no network requests.
- Exclusions accept plain phrases, `path:blog` for path-only matching, and `domain:x.com` for exact-host/subdomain matching. Simple plural and hyphen/space variants are supported. The optional contact-discovery preset appends common noise filters. Saving a campaign recalculates existing URL scores while preserving review decisions, cancels the previous unfinished run and leaves the campaign paused. Set page priority around 35 after reviewing filters and the resulting URL list. Automatic new-site setup has a separate policy threshold, default 70, and additional scope/domain/budget checks.
- Exact page paths and meaningful query parameters are retained. Tracking parameters/fragments are removed, while obvious login, calendar, filter, session and asset URLs are excluded. Limits stop cyclic links and unbounded expansion.
- New domains are recorded from links/search metadata. They can be authorized by an operator or an enabled campaign policy and attached to the campaign. Policy authorization creates a paused Source for bounded setup, not immediate normal crawling (when its canary passes, its approved URLs join the open run through the ordinary gates), and it cannot authorize generic collateral without direct contact-page intent regardless of contextual score. DNS/public-network guards, source authorization, the current page threshold, and direct intent are checked again immediately before an ordinary queued page is dispatched, so a stale job cannot bypass changed campaign eligibility. No policy may silently widen an existing source's paths.
- Sitemap URL sets, nested indexes, and gzip files are supported. Compressed and decompressed bodies are capped at 2 MiB, parsing disables DTD/entities/external references, each map considers at most 2,000 entries, and nested indexes are limited to depth 3. Sitemap and page URLs must remain in approved scope. A `/team`-only source does not authorize fetching `/sitemap.xml`; robots-listed maps outside scope are ignored.
- Discovery uses HTML fetches. JavaScript-only pages may need the existing browser collector and a source recipe separately. No model is installed or enabled by this feature; existing extraction settings still apply.
- Source business categories and person/page service evidence retain their existing meanings. Campaign relevance never becomes a person's service tag. Search snippets never create contact records.
- Daily fetch/search counters survive restarts and pause/resume, using UTC days. A fetch attempt includes a robots check or a page/sitemap request; redirect hops are additionally bounded by the transport. Retries count. A crash can conservatively consume an attempt even when no response was saved. Offline demo pages do not consume network quotas.
- Limits apply per campaign; regular source runs have their own limits. Search also has a shared application-wide daily cap. One worker alternates discovery and normal collection, and rotates between runnable campaigns. It uses the existing per-origin throttle and crash-recovery lease.
- Saved URL caps do not delete history automatically. Raising the cap allows further discovery. Only the latest discovery context/referrer is retained per URL; this is not a complete historical web graph.

## Optional Brave web search

Link/sitemap discovery expands from starting sources. Search can find sites that are not linked from them. Enable only if you have an appropriate Brave Search API account/key. Usage is subject to the provider's plan and terms.

Add these settings to your private environment/configuration and restart the app:

```dotenv
BRAVE_SEARCH_ENABLED=1
BRAVE_SEARCH_API_KEY=your-private-key
BRAVE_DAILY_SEARCH_LIMIT=20
```

Then enable search in the campaign and enter exact queries, for example:

```text
"merchant services" "sales representatives" California
"POS reseller" "our team"
```

Each run performs one result page (up to 20 results) per configured query, up to 10 queries. There is no query generation or paginated search in this pilot. Include your desired region in the query; the campaign's region hint does not rewrite it. Search-only campaigns are supported.

The adapter uses the [Brave Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get), not its AI answer endpoint. It fixes the destination, disables redirects for API-key requests, bounds responses, and handles rate limits with persisted backoff. Keys stay in the environment and never appear in the dashboard. Both the campaign and shared daily cap must permit a request; failed attempts also count. This is a request cap, not a dollar-based billing limit.

The optional search adapter has deterministic contract tests. No live paid API request or real lead-site crawl was performed in this workspace; validate a small real campaign on your desktop before relying on its yield.
