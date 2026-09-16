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

Approval attaches that source to the campaign. Matching URLs can enter the current run if capacity remains, or the next run. Dismissed URLs stay dismissed until explicitly approved individually. You can dismiss all URLs for one exact origin within a campaign.

Campaigns use their own start/pause and page/depth limits; a source's regular collection schedule can stay off to avoid duplicate fetching. Source origin/path restrictions, robots policy and delays apply to both workflows. Pausing or editing an operator-configured source pauses its active campaigns. Automatic source failures pause that site's setup without stopping other campaign sources. Editing a campaign cancels its old unfinished run and leaves it paused. Legacy in-flight pages may finish saving; automatic setup rechecks its authorization and generation before committing results.

## Ranking and boundaries

- Team/contact/sales/agent/partner/representative/executive/directory terms in the URL path or link label: +35; campaign industry phrase: +25; sales-role phrase: +20; region phrase: +10. Nearby context can support industry/role ranking but cannot give unrelated product links the team-page bonus. Navigation/header/footer links use their own labels as context. Editorial/legal/recruitment paths or labels reduce priority by 20; product/device/software paths or labels reduce it by 25. Sources with human-reviewed contacts can receive a +10 preference when URLs are discovered. Scores are priority signals, not confidence percentages. An enabled policy can use them to authorize a bounded probe; recipe release has separate evidence gates.
- Exclusions accept plain phrases, `path:blog` for path-only matching, and `domain:x.com` for exact-host/subdomain matching. Simple plural and hyphen/space variants are supported. The optional contact-discovery preset appends common noise filters. Saving a campaign recalculates existing URL scores while preserving review decisions, cancels the previous unfinished run and leaves the campaign paused. Set page priority around 35 after reviewing filters and the resulting URL list. Automatic new-site setup has a separate policy threshold, default 70, and additional scope/domain/budget checks.
- Exact page paths and meaningful query parameters are retained. Tracking parameters/fragments are removed, while obvious login, calendar, filter, session and asset URLs are excluded. Limits stop cyclic links and unbounded expansion.
- New domains are recorded from links/search metadata. They can be authorized by an operator or an enabled campaign policy and attached to the campaign. Policy authorization creates a paused Source for bounded setup, not immediate normal crawling. DNS/public-network guards and source authorization are checked at execution time. No policy may silently widen an existing source's paths.
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
