# Discovery follow-up, 2026-10-23

Items inspected while fixing fresh-URL admission. They are not part of the queue fix because a
code change would be broader than the reproduced failure, or the safe behavior is not yet proven.
No production data was used. Do not treat this note as authorization to change live budgets,
approvals, or the droplet.

## Persistent Common Crawl host retries

A host that returns HTTP 504 is retried once inside the command and then skipped. The summary now
says so. Nothing records those hosts for the next scheduled lookup, so the next run queries every
approved source again, including hosts that answered.

A bounded follow-up would store the host, collection, status, and UTC time of the skip, then let
one later run query only those hosts before the normal rotation. Cap the stored list (the index
lookup is already limited to 25 sources) and drop entries after a successful answer or after a
fixed number of skips. Do not add a second worker, and do not retry from the collector lease.

## Brave empty results

Search 17 in the 23 October snapshot returned HTTP 200 with an empty result list and still
consumed one campaign search and the full interval. The provider call succeeded. Rotating to
another query immediately would spend a second daily search. This pass does not change Brave
quotas, intervals, or the provider client.

A later change can record the HTTP status already observed and advance the query cursor after an
empty 200 without raising `daily_search_limit` or `BRAVE_DAILY_SEARCH_LIMIT`. A transport failure
should keep the existing persisted backoff.

## Zero-yield allowance inside a run

`run_allowance_spent` allows five non-seed page fetches per run on an origin with at least ten
fetched pages and no stored contact. Jobs already in the run are not replaced when a later one is
skipped, so an older equal-priority page can consume the allowance before a newly queued direct
page on that same origin. The allowance is per run and does not dismiss the URL. Changing which
job runs first, or freeing a skipped slot for another origin mid-run, needs its own test against
a full run. This pass does not raise the allowance.

## Job boards and marketplaces

`path:jobs`, `path:blog`, `path:article`, and the editorial penalty still reject the fixtures
covered by the new tests. A job-board URL whose path contains a direct-intent phrase and whose
pre-yield rank still meets `min_score` remains eligible; the yield penalty does not veto it.
Excluding hosts such as marketplace or job-board domains is an operator exclusion or a reviewed
source-scope change, not a lower global threshold. No host list was added here.

## Policy sources paused at `no_matching_cards`

The 23 October snapshot showed two policy admissions paused because the probe found no person
cards. The pause is the existing setup gate and was not changed. A campaign list can later say
"no person cards on the probed page" and omit those sources from collecting counts. That display
change was not required to get index URLs into the queue.

## Run reason versus run status

A run that saved pages and also had failed jobs now finishes `completed` and keeps
`terminal_reason=job_failures` plus the failure counts. Readers that treat every
`job_failures` reason as a failed run will still need to look at `status`. Renaming the reason
would be a separate compatibility change.
