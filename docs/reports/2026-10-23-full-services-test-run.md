# Full-services test run, 2026-10-23

Read-only snapshot of the live checkout at `16d4e35`. No contact details.

## Run summary

The Common Crawl oneshot finished cleanly. It started at 14:07:01 PDT and exited 0 at 14:13:30 PDT, about 6.5 minutes, on collection `CC-MAIN-2026-43`. It registered 263 in-scope contact-page URLs and added 164 new ones to campaign 2, across 14 origins.

| Origin | New URLs |
|---|---|
| `https://builtin.com` | 31 |
| `https://www.gorspa.org` | 28 |
| `https://salesfolks.com` | 20 |
| `https://www.salesjobs.com` | 19 |
| `https://www.commissioncrowd.com` | 15 |
| `https://www.paymentsjournal.com` | 13 |
| `https://www.freelancer.com` | 9 |
| `https://www.digitaltransactions.net` | 8 |
| `https://www.rephunter.net` | 8 |
| `https://www.joinpavilion.com` | 5 |
| `https://www.manaonline.org` | 3 |
| `https://www.rainmakers.co` | 3 |
| `https://hyperhired.com` | 1 |
| `https://www.vendux.org` | 1 |

Eight approved sources got an index HTTP 504. Each was retried once inside the run and then skipped, with no URLs registered: Host Merchant Services, Team Merchant, Florida Merchant Services, Wells Fargo, The Green Sheet, the Electronic Transactions Association, Card Payment Options, and SalesAgent.

Discovery run 194 then failed at 14:39 PDT (`terminal_reason=job_failures`) without fetching any of those 164 URLs. Of the 164: 67 were dismissed at intake, 42 were parked because the origin already held its active-URL share, and 55 were approved but never given a job. The run's other work saved 41 pages, failed 22 jobs, and stored 0 accepted observations. Leads stayed at 21. No new lead was created after 12:21 PDT.

As of 14:14 PDT, Jev, Brave, and Common Crawl were on together, with one collector worker and no crash loop. The 14:45 PDT snapshot still showed one worker (PID 2577003) and `NRestarts=0`.

Brave, from that 14:45 snapshot: campaign 2 had used 2 of 5 searches, and the global total was 2 of 20. Search 16 at 13:09 PDT returned HTTP 200, 3 results, and 2 new URLs. Search 17 at 14:09 PDT returned HTTP 200 and 0 results. The next search was 15:09 PDT.

Jev, from that same snapshot: 169 attempts since 19:51Z, every one settled with HTTP 200. Spend was 6,884,514 nUSD of the $2 daily allowance (2,000,000,000 nUSD), with no open reservation and no error code.

Sources were 169 total and 145 approved, up from 167 and 143. The two additions are policy admissions, both paused: `javelinstrategy.com` (13:41 PDT) and `salessage.co` (14:15 PDT).

The weekly timer was left enabled. Its next firing is Mon 2026-10-26 04:15:32 PDT. After the oneshot, run 194 was already `failed` and none of the 164 URLs had a queued, pending, leased, or running job, so nothing was cancelled.

## Improvements for next version

### Run 194 was marked failed because any job failure fails the run

**Verified.** `discovery_discoveryrun` 194 has `status=failed`, `terminal_reason=job_failures`, and the message `22 job(s) failed (fetch_error=18, parser_or_extraction_error=3, worker_error=1). 41 pages saved; 0 accepted observations; 0 new contacts (all collectors).` `finish()` calls `terminal()`, which sets the code to `job_failures` whenever the failure count is non-zero, and then writes `status=failed` even though pages were saved.

**Suggestion.** Keep the failure counts, and mark a run `completed` with a warning when some jobs failed but the run also saved pages. Reserve `failed` for a run that could not do its work.

### 55 approved URLs never got a job

**Verified.** All 55 are on `rules_only` sources, so `collection_allowed()` does not block them. 37 have a stored score under the campaign minimum of 35 (mostly 20 or 0). The +35 direct-contact bonus is cut by a −15 yield penalty ("site stored no validated contacts"), which drops them under the gate in `queue_candidate()` before a job is created. The other 18 score 35 or 40, name a direct contact page, and still have no job. Campaign 2's `max_pages` is 500, and run 194 holds exactly 500 jobs, none of them these URLs. `discovery_commoncrawl --start-if-idle` calls `start()` first, and `start()` fills the run from remembered pages, seeds, and sitemaps up to `max_pages` before `register_common_crawl()` runs.

**Suggestion.** Register the index batch before remembered inventory takes the cap, or reserve a slice of `max_pages` for it. Do not treat a below-minimum score as an approved URL that looks queued; leave it pending or record that the yield penalty kept it out.

### 164 URLs produced no leads

**Verified.** None of the 164 were fetched, so none could produce a lead. Intake split them three ways:

- 67 dismissed, all at score −100, for an excluded path phrase: `path:article` 37, `path:blog` 24, `path:product` 4, `path:software` 4. All 31 `builtin.com` URLs were dismissed as articles.
- 42 parked with the origin-share reason. `salesfolks.com` 20, `salesjobs.com` 19, `freelancer.com` 3.
- 55 approved and not queued, as above.

The run's separate 41 saved pages also stored 0 candidate people and 0 observations. That is the run, not these 164 URLs.

**Hypothesis.** Directory, job-board, and article hosts are poor lead sources for this campaign. The index can see "team" or "sales" in a path and still return posts and marketplace listings. `builtin.com` contributed the largest pile and every one of those URLs was an article. `salesjobs.com` and `freelancer.com` were parked or dismissed rather than fetched.

**Suggestion.** Exclude known article and marketplace hosts from the index lookup, or require the URL's own path to be a team/people page before it counts as in-scope. Apply the origin-share park before counting a URL as a new contact-page find in the run summary.

### 22 job failures

**Verified.** On run 194: 16 page `fetch_error`, 2 sitemap `fetch_error`, 3 sitemap `parser_or_extraction_error`, 1 sitemap `worker_error`. Of the messages, 3 name HTTP 404. The other 15 fetch failures, and the parser and worker failures, only say "Request or processing stopped; review source and configuration." Stored diagnostics show 44 pages fetched, 0 candidate people, and 0 candidate contact methods. 389 other jobs were skipped without a request, mostly because the per-source sitemap allowance (5) or the zero-yield page allowance (5) was already used.

**Suggestion.** Persist the HTTP status or exception category on the job without the response body, so a 404 is distinguishable from a block or a timeout. Don't let those skips and failures be the only outcome of a run whose new index URLs never entered the queue.

### Common Crawl index HTTP 504

**Verified.** `common_crawl_specs()` retries a retryable `FetchError` once per host and then records `index error: HTTP 504; retry scheduled.` with a kept count of 0. The words "retry scheduled" come from `FetchError` for a retryable status. Nothing is queued for later. The oneshot still exits 0.

**Suggestion.** Say "skipped after one retry" in the host line. Store the eight skipped hosts and let a later run retry only those hosts, instead of looking like a retry was scheduled when the process has exited.

### The oneshot does not take the collector lease

**Verified.** `discovery_commoncrawl.Command.handle` never calls `acquire_lease` or reads `WorkerLease`. It calls `start()`, which queues up to `max_pages` and then `finish()`, and then `register_common_crawl()`, which inserts URLs under a campaign row lock only. The collector worker can be fetching the same run at the same time. This run was accepted as a one-shot beside `crono-lead-engine`; the lease gap is what let `start()` fill the 500 slots before the new URLs were considered.

**Suggestion.** Hold the collector lease across `start()` and `register_common_crawl()`, or refuse `--start-if-idle` while the lease is fresh and only append index URLs to a run the worker already owns. Reserving queue space for the index batch matters more than a second worker, because this process does not fetch the target sites.

### Two policy sources paused at `no_matching_cards`

**Verified.** Automation jobs 107 (`javelinstrategy.com`, admitted 13:41 PDT) and 108 (`salessage.co`, admitted 14:15 PDT) are `paused` with `Recipe review needed: no_matching_cards. No contacts were stored.` Both sources are approved and inactive. `recipes.evaluate()` returns `no_matching_cards` when the matched card total is 0, and the setup path pauses on that outcome instead of releasing a recipe.

**Suggestion.** Keep the pause. Surface these as "no person cards on the probed page" in the campaign list, and don't count them as collecting sources. A homepage with no team block should not sit approved waiting for a recipe that the probe already showed it cannot support.

### Brave search 17 returned nothing

**Verified.** Search 17 finished `done` at 14:09 PDT with `results=0`, `invalid=0`, `filtered=0`, and `new_urls=0`. The activity log for that minute is one Brave fetch, HTTP 200, 544 bytes. Search 16, an hour earlier, was HTTP 200 with 3 results and 2 new URLs. The empty search still consumed one of the five daily campaign searches and moved the next search to 15:09 PDT.

**Suggestion.** Store the provider status on `SourceSearch` (it is already 200 here) and treat a 200 with an empty result list as an empty query, not a failure. A zero-result query should rotate immediately to the next query instead of waiting out the full hour, so one empty answer does not spend both time and a daily slot the same way a useful search does.
