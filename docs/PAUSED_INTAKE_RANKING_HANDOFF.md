# Paused intake ranking handoff

The operator stopped this deployment on October 2, 2026 Pacific time after it produced no new leads following the intake-ranking upgrade. This branch preserves the deployed application plus this handoff. The next work should improve candidate relevance and repeated crawling, with measured tests before another production trial. The recommendations below have not been implemented.

## Preserved version and shutdown

| Item | Recorded state |
| --- | --- |
| Preservation branch | `handoff/intake-ranking-paused-2026-10-02` |
| Application commit | `10f37e8538e634ad46b000a8db49f5475e65e4d2` |
| Previous production commit | `b2161bc7a32b987f9bf411d260eae9ca5dbc466d` |
| Shutdown verified | October 3, 2026 at 03:11:35 UTC; October 2 at 20:11:35 Pacific |
| Lead engine | `crono-lead-engine.service`: disabled, inactive, `MainPID=0`, empty cgroup |
| Jev timer | `crono-jev-soak.timer`: disabled and inactive; a timer has no MainPID |
| Jev soak | `crono-jev-soak.service`: static, inactive, `MainPID=0`, empty cgroup |
| HTTP endpoint | `127.0.0.1:8017/login/` no longer accepts connections |
| Database holders | None after shutdown |

The shutdown stopped and disabled the per-user systemd service and timer. It did not pause campaigns in the database, change their approvals or policy settings, or remove queues. Campaign 2 remains `active=True`, with no open discovery run at shutdown. **Starting the service again can immediately resume scheduled collection and paid search.** Leave it off until the operator requests another production run.

The deployed checkout remains `/home/crono/dev/crono_crawler-deploy-10f37e8`. The preservation worktree is `/home/crono/dev/crono_crawler-handoff-2026-10-02`. Application code on this branch is identical to `10f37e8`; only repository documentation was added or updated.

The effective service drop-in remains `~/.config/systemd/user/crono-lead-engine.service.d/99-intake-ranking.conf`. Jev's soak service still uses its own older checkout and database. Do not repoint it to the lead-engine database.

## Deployment and settings retained

The production deployment applied only `automation.0006_intake_ranking` and `discovery.0010_intake_ranking`. Migration planning, migration execution, Django checks, migration drift and static collection passed. Backup counts and job states matched a fresh baseline captured after the worker stopped. Startup verified HTTP 200, one launcher, one web process, one worker and a fresh collector lease. The earlier reviewed build had 681 passing tests and five passing offline smokes; those were not rerun for this documentation-only preservation.

Campaign 2 retains:

| Setting | Value |
| --- | --- |
| `SitePolicy.explore_share` | `20` |
| `Campaign.search_precision` | `True` |
| `SitePolicy.min_url_score` | `70` |
| `SitePolicy.max_sites_per_day` | `25` |
| `SitePolicy.require_page_intent` | `False` |

The generated query pool had exactly 20 queries, each containing `intitle:`. The deployment manually triggered exactly one discovery run, #166, and restored its future `next_due_at`. It never wrote `next_search_at`; later runs and searches were scheduled normally by the worker. Budgets, quotas, source permission, human decisions, Brave/Jev configuration, origin caps and parking settings were not changed by this deployment or shutdown.

## Production observations

At shutdown there were **21 leads: 14 with evidence from real sources and 7 demo-only records**, plus 21 observations, 6,529 discovered URLs and 19,067 discovery jobs across the database. No new real-source leads were added after deployment. The newest real-source leads were first seen September 30 at 10:19 Pacific and used shared business contact details. A refreshed observation of an existing person is not a new lead.

Campaign 2 had 101 setup jobs: 2 active, 76 paused and 23 failed. At the first UTC reset after the setting change, ranked intake admitted one direct candidate and five generic candidates. All six paused. Examples of their recorded outcomes included no people listed, missing usable contacts, contacts rejected, an access challenge and unusable robots content. Several assessments found named people but recorded zero sales titles. These observations do not establish whether a particular selector missed evidence or the site was unsuitable.

The last full crawl, #184, started October 3 at 02:24 UTC and finished at 02:49 UTC:

| Outcome | Jobs |
| --- | ---: |
| Successful pages | 35 |
| Successful sitemaps | 51 |
| Failed pages | 7 |
| Failed sitemaps | 8 |
| Skipped pages | 157 |
| Skipped sitemaps | 242 |
| Total | 500 |

It saw 8 existing contacts and created 0 new contacts. Compared with full run #181, 29 of its 35 successful page URLs repeated, and 14 of its 15 failed jobs repeated the same URL and error. Of its 399 skipped jobs, 235 hit the existing sitemap allowance, 157 hit the existing zero-yield source allowance, and 7 had no sitemap at that address. Skipped jobs generally made no request when an allowance was exhausted; the queue count is not the HTTP request count.

Repeated failures included 404s, oversized responses or sitemaps, redirect loops, redirects outside approved scope, unsupported ports and invalid XML. In `discovery.services.finish`, any failed job makes the entire run's status `failed`. That status alone does not mean the worker crashed. Run #185 was a later search-ingestion run, completed with zero pages; it was the latest run at shutdown.

Precision search did execute after deployment, but its results included staff-management documentation, recruiting guides and company names containing “Team.” Some results were correctly filtered; other unsuitable-looking metadata remained pending. A title match does not establish a people directory, a sales role or permission to fetch. [Brave documents](https://api-dashboard.search.brave.com/documentation/resources/search-operators) `intitle:` as a title-word operator and describes search operators as experimental.

## Recommended next changes

### Improve recognition of people pages

Review the false positives in stored search metadata and create synthetic fixtures representing support manuals, staff-management software, recruiting guides, incidental company names and genuine sales-team listings. Improve contextual page classification in `discovery/ranking.py` and query evaluation in `discovery/acquisition.py`. Keep industry relevance, page intent and source authorization distinct. Do not blanket-exclude every occurrence of words such as “staff” or “support.”

Acceptance should include fewer false direct-intent matches on held-out examples while retaining genuine team/profile pages. Measure search-to-eligible-page and setup-to-new-contact conversion within the existing search allowance. Any later query configuration change should be a separate operator-reviewed choice.

### Persist refresh eligibility and failure cooldowns

Add durable per-URL refresh scheduling so a new campaign run does not immediately repeat unchanged pages or stable failures. Distinguish transient failures from recurring 404s, size failures and scope problems. Preserve periodic checks and reconsideration after relevant configuration changes; a cooldown must not dismiss a source, widen scope or erase evidence. Examine `discovery/services.py`, the job/candidate models and `leads/services/network.py`.

Prefer eligible unseen pages when refreshes are not due. Optional HTTP validators can reduce response transfer where servers support them, though conditional requests still consume requests and must use the existing network guards. See [HTTP conditional request semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.2).

Test that cooldowns survive restart, respect source/policy changes, retain the single-worker lease rules and preserve human decisions. Measure repeated fetches and new leads per request; reduced traffic alone does not prove improved lead yield.

### Account for existing allowances when scheduling

Run #184 filled 500 job positions but skipped 392 because existing per-source allowances were exhausted. Model those same allowances when constructing the queue, including jobs already queued, and keep the dispatch-time checks authoritative. Preserve current seed/follow-up exemptions, source fairness and the reservation for never-fetched pages. Do not change the limits to make the queue look busier.

Relevant code is `discovery.services.start`, `queue_candidate` and `run_allowance_spent`. Test that known-unexecutable work cannot crowd out eligible work, including after a restart and when several jobs share one source.

### Fix extraction only where evidence supports it

Review paused setups that found named people and contact information. Distinguish unsuitable content from missed card boundaries, titles and personal contact associations. Add source-specific recipes or general parser fixes only when the source evidence supports the requested sales lead. Continue to distinguish personal contacts from shared business contacts, retain unknown attributes, and preserve suppression decisions.

Use the existing local recipe validation and bounded canary. Never release a recipe on ranking alone. Probe HTML expires after 24 hours, so future work must not assume the original private pages are still available; any new fetch needs existing authorization. Regression fixtures must use fictional people and contact details.

### Explain outcomes and admission decisions

Display successful, failed and skipped work separately, and distinguish partial completion from a worker/service failure. A “completed with errors” presentation should preserve the underlying error details and account for existing status consumers. Add an explicit explanation for each pending candidate's admission blocker, such as scope, an existing source, policy restrictions or exhausted exploratory slots.

Clarify the policy interface: with `require_page_intent=False`, `automation.policy.admission_gate` rejects negative scores but does not enforce `min_url_score=70` as a floor. This is current behavior, not a newly changed threshold. Fix the explanation first; changing admission semantics requires a separately reviewed decision.

Show the conversion path from search result to eligible page, setup, released recipe and new real-source lead. Refresh counts and query-result counts should remain visible as distinct measures.

## Local state and backups

These paths refer to the operator's machine; they are not files shipped in this GitHub branch.

- Production database: `/home/crono/dev/crono_crawler/data/leads.sqlite3`.
- Maintenance directory: `/home/crono/backups/crono_crawler/20261001T142542-0700-campaign2-intake-ranking-deploy`.
- Frozen shutdown image: `private/leads-paused-20261003T031135Z.sqlite3` under that maintenance directory. Backup API image, `integrity_check=ok`, counts equal the stopped production database. SHA256: `31558eab4956d45d263a6565f40e1d5a64d57cccc4882e97e9e10ef93d201e1d`.
- Pre-migration image: `private/leads-after-stop.sqlite3`. SHA256: `59d2e903fdfecbf1fe8b2bb373e464f0d2b6d039b6b48f91dbd40af133236ce3`.
- Reports: `reports/deploy-report.txt`, `reports/shutdown-handoff-state.json`, `reports/paused-backup-verification.json`, intake/audit/inventory previews, the canonical requeue dry run, and deployment journal captures.

No runtime database, credentials, contact export, browser profile or collected HTML belongs in Git. The original deployment's canonical requeue dry run found no eligible jobs; no requeue apply was performed. Do not restore a database backup merely to change code.

## Starting the next work session

1. Read `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/DISCOVERY.md`, `docs/AUTOMATION.md` and `docs/INTAKE_RANKING_HANDOFF.md`. That earlier handoff describes the original pre-deployment work; this document records the subsequent production observations and shutdown.
2. Verify the services remain off and compare the production checkout and database state with this handoff. Keep exploratory commands and local reports in the maintenance directory. The deployed checkout should remain unchanged.
3. Prepare implementation in an isolated development checkout following the repository's `main`-base convention. This preservation branch deliberately starts at the exact deployed commit, so compare it with current `main` before selecting changes to carry forward. Never merge work into `archive/legacy-scraper-2026-09-16`.
4. Run deterministic regression tests, Django checks and migration-drift checks for code changes, plus relevant offline smokes. Use an isolated environment and synthetic database. Before any intentionally authorized production management command in the deployed checkout, unset `DATA_DIR`, `DATABASE_URL` and `DJANGO_SECRET_KEY`, then assert the resolved database is exactly the production path above.
5. Resume production only on a new operator instruction, with a reviewed change and explicit outcome measurements. Use per-user `systemctl --user`, verify the effective checkout before starting, then HTTP 200, one launcher/web/worker and a fresh collector lease. Enabling the timer can start the soak service and its dependency; preserve its separate older checkout/database.

No improvement implementation, production restart, budget change, threshold change or source approval is authorized by this handoff itself.
