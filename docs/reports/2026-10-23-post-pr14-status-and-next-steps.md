# Post-PR #14 status and next steps, 2026-10-23

Status note for agent review. It records what shipped in PR #14, what the live checkout did after the 2026-10-23 deploy, and follow-ups that have not been started. No code, config, budget, or approval change is authorized by this note.

## PR #14 delivery

PR #14 (fresh-URL queue and yield priority) merged to `main` as `0b15802` from head `29140b7`. Codex fixed two re-review follow-ups before that merge:

- `recipe_review_runs` excludes `job_failures` inside the latest-run subquery.
- An ineligible return clears only `QUEUE_FULL_RESULT`.

The pop-os isolated worktree retest passed: 787 tests OK, plus Django `check`, `makemigrations`, the funnel check, four smoke scripts, and `git diff --check`. An independent re-review returned GO, with no new findings.

The change was deployed to the pop-os live checkout on 2026-10-23 at 21:06 PDT. The deploy ran one worker, applied no migrations, and left `NRestarts=0`.

| Checkpoint | Detail |
| --- | --- |
| Annotated tag | `checkpoint-2026-10-23-pr14` on `0b15802` |
| Pre-deploy database backup | Integrity ok, stored in the pop-os checkpoint directory, with rollback steps to `c3a16e2` |
| Brave | On |
| Jev | Live |
| `JEV_ROUTING_ENABLED` | `0` |
| Caps | Unchanged: Brave 5/day on campaign 2 and 20 global; Jev $2/day |
| Common Crawl weekly timer | Next run Monday 2026-10-26 at 04:15 PDT, on the new code |

## Results since deploy (through 21:16 PDT)

In the ten minutes after deploy there were 0 new leads, 0 observations, and 0 page jobs fetched. Discovery run 204 completed empty. Its Brave search returned HTTP 200 with 0 results, and campaign 2 had already hit 5/5 searches for the UTC day. There were 0 Jev attempts in that window.

## Whole day, 2026-10-23 PDT

Accepted leads created: 0. Evidence documents: 366, containing 209 contact-candidate rows. None of those candidates became leads.

### Collection runs

26 collection runs:

| Outcome | Runs | Pages | Contacts |
| --- | ---: | ---: | ---: |
| Completed | 22 | 173 | 0 |
| Failed | 4 | 8 | 0 |

Five older paused collection runs were left untouched.

### Discovery runs

19 discovery runs:

| Outcome | Runs | Pages | Contacts |
| --- | ---: | ---: | --- |
| Completed | 12 | 0 | — |
| `refresh_only` | 1 | 2 | 1 seen, 0 new |
| Failed, `job_failures` | 6 | 247 `pages_done` | 0 |

Brave completed 9 searches, mostly with 0 results.

Jev made 354 attempts. All returned HTTP 200. Spend was 14,449,218 nUSD, well under the cap, and produced zero leads.

### Common Crawl run 194

Run 194’s set held 161 URLs: 94 approved and 67 dismissed. Before the deploy, that batch had 1 job done, 9 fetch failures, and 33 skipped with no request. None of those URLs are queued now. Top origins in the batch included `builtin.com` (a job board) and `gorspa.org`.

## Diagnosis so far

The queue fix admits fresh URLs, but the pages that were fetched yielded zero contacts. The bottleneck is now targeting and/or extraction, not queueing.

## Proposed next steps

These steps have not been started. They are listed for review.

1. Read-only diagnosis of the day’s roughly 430 pages. Bucket zero-contact pages into empty or JS-only, block or challenge page, wrong page type (blog, job board, product), or a real team or contact page missed by extraction. Trace why the 209 contact candidates were rejected.
2. Fix the largest bucket. Use extraction fixtures and parser tuning if real team pages are missed. Tighten source or path targeting if the pages are the wrong type. Use the existing render path for JS pages. Blocked sites are skipped and never bypassed.
3. Gate Jev to pages that already show contact signals, cutting spend on empty pages without touching the $2 cap.
4. Source quality: down-rank or operator-exclude job boards and marketplaces such as `builtin.com`. Review Brave queries that keep returning 0 results. Do not increase the quota.
5. Investigate the low Common Crawl fetch success (33 skipped, 9 failed): `robots.txt`, dead links, or timeouts.

## Constraints for any follow-up

- `robots.txt` is never disabled.
- No challenge bypass and no unapproved scraping.
- Budgets, caps, thresholds, and approvals stay unchanged.
- One collector worker per SQLite database.
- Tests run only on isolated worktrees or temporary databases, never on the production database.
- No secrets in the repository.
