# Local Jev repair and deployment handoff

Repair completed September 22, 2026 (America/Los_Angeles; verification logs are September 23 UTC). **Offline verification only. Deployment, live discovery and paid calls remain unauthorized.** The reported $5 account balance is not a spending allowance.

## Revision and preserved work

- Checkout: `/home/crono/dev/crono_crawler-jev-review`; branch: `feat/jev-local-rollout`.
- Exact base and final HEAD: `a32c1a9253ace142cc2814db930b1ec632dfcf00`. No commits, rebase, reset, remote writes or branch changes were made during this repair. The final implementation is the reviewed working-tree diff on this HEAD, **not the unchanged HEAD alone**. It retains upstream Jev commit `125b7c455d370af07625250b26dfab687956411b` and the extraction additions.
- Interrupted changes were preserved before editing in `.repair-checkpoints/20260923T011642Z/interrupted.patch`, with copies of the three untracked source/test files and the original git status. The checkpoint contains no environment files, runtime databases or credentials and is now ignored by Git.
- Final working tree deliberately remains uncommitted. Modified files: `.gitignore`, `automation/policy.py`, `classification/{accounting,contracts,evidence,fixtures,models,provider,routing,services,views}.py`, `classification/management/commands/jev.py`, `classification/tests/test_jev.py`, `discovery/services.py`, `scripts/smoke_jev.py`, both classification templates, and `docs/JEV_TESTING.md`. New files: `classification/migrations/0005_jevcontrol_cumulative_attempt_limit.py`, `classification/tests/test_rollout.py`, `discovery/importing.py`, `discovery/management/commands/import_discovery_candidates.py`, `discovery/tests/test_importing.py`, and this handoff.
- Before packaging/deployment, inspect `git status --short` and `git diff --check`, then commit this exact source/tests/migration/docs set locally. Do not deploy just the base commit or include ignored checkpoints, test results, the prepared research metadata, `.env`, databases or virtualenvs.

The production working tree, private configuration, production database, service and private `docs/JEV_INTEGRATION_PLAN.md` were not accessed or changed. The production branch/commit/bind supplied in the task are historical operator observations, not freshly verified facts.

## Confirmed defects, repairs and evidence

Tests below are in `classification/tests/test_rollout.py` unless otherwise specified. All provider responses used for testing are stubs.

| Confirmed issue | Repair | Regression evidence |
| --- | --- | --- |
| A list/dict winning choice raised `TypeError`, escaping contract handling | Require a string choice before dictionary membership/indexing | `test_unhashable_choice_is_contract_failure_and_usage_still_settles`; malformed-response matrix |
| An enormous integer probability raised `OverflowError` in `isfinite` | Check type and range before finite conversion | `test_large_numeric_probability_is_contract_error` |
| Existing evidence remained spendable after its campaign policy was revoked | Recheck current policy, snapshot, membership and scope at capture, admission and dispatch | `test_revoked_policy_blocks_paid_admission_of_existing_evidence`; `test_dispatch_rechecks_revoked_policy_after_reservation` |
| Doctor reported readiness without an effective money gate and threw an exception on inconsistent accounting | Shared read-only diagnostics cover config, ledger, pause, dispatch owner, UTC-day full-reservation allowance and cooldown; doctor emits `ledger_consistent=false` safely | Missing-daily-allowance and inconsistent-ledger tests |
| Timeouts and retryable HTTP responses with unknown usage automatically re-entered the send queue | Unknown billing retains the full reservation and becomes `uncertain`; admission also blocks legacy unresolved attempts. Only explicit audited recovery permits retry | `test_uncertain_transport_never_automatically_retries`; `test_legacy_retry_wait_with_uncertain_attempt_requires_explicit_recovery` |
| Legacy attempt history could stop otherwise affordable daily work | Retain historical counts for audit but remove them from admission | `test_attempt_history_survives_days_without_becoming_an_admission_stop` |
| Lowering the UTC-day dollar limit after reservation did not revoke the unsent permit | Dispatch rechecks current-day exposure against the effective daily limit | `test_lowered_daily_allowance_revokes_unsent_permit` |
| Reconciling an old attempt changed a newer active evaluation to `waiting` | Only the latest attempt can reschedule its evaluation; reject repeated/no-op recovery; retain backoff and dispatch owner | `test_reconciling_old_attempt_never_disturbs_new_dispatch` |
| Calibration drift could be accepted or a changed fixture URL replaced; supplied arbitrary HTML could be labelled synthetic | Fixed source configuration/body/hash checks, previous synthetic-source check and no restoration of revoked/edited permissions | Synthetic fixture change, URL edit and body-contract tests |
| Retention fallback could select a fetched document for a synthetic fingerprint | Include provenance in retained-document lookup; preserve the original hashes/parser/signature/timestamp | `test_expired_synthetic_cannot_reuse_fetched_provenance` |
| Demo/mock company judgments could choose a real source in an existing campaign; existing jobs did not recheck their trigger | Synthetic calibration is ineligible for routing. Mock/demo routing is restricted to its own fixed offline source. Recheck trigger evidence and target at queueing, advancement and each work reservation | `test_mock_result_cannot_select_real_source_or_escape_after_source_edit`; `test_synthetic_judgments_cannot_route_even_with_verified_domains_and_campaign` covers cached/reused/live-provider fixture evidence too |
| Company advancement could add candidates beyond the inventory cap | Lock the campaign and distinguish known candidates from new inventory before creation | Two company-routing capacity tests |
| Model domain proposals could leave existing pending entries eligible for automatic onboarding at full inventory | Mark existing pending proposals for manual review even at capacity; retain approvals/dismissals, validate URL and honor origin dismissals/exclusions | `test_existing_model_domain_proposal_stays_manual_even_at_inventory_cap` |
| An origin dismissal did not stop newly found URLs on an already approved source | Registration blocks approved-source expansion and company advancement blocks new/unapproved URLs on dismissed origins; explicit exact-URL approvals and source-less pending metadata remain preserved | Registration/company origin-dismissal regressions; `red-origin.log` |
| Import diagnostics echoed URL query/contact values; raw controls/nested JSON were not uniformly rejected | Validate raw metadata before normalization, bound parsing, redact outcomes to row numbers and status | `discovery.tests.test_importing.ImportTests` |
| A previously loaded policy candidate could bypass a newly saved import-review gate | Policy consideration locks the same campaign as import/registration and reloads the candidate decision | `test_stale_policy_candidate_cannot_bypass_import_review` |

The four originally requested failures were reproduced against an untouched `git archive` export of the exact base in `test-results/repair/baseline`, using this checkout's virtualenv and fresh test data. Result: **4 tests, 2 failures and 2 errors** (`red-base.log`). Further regressions failed against the interrupted implementation (`red-extra.log`, `red-import.log`). The old-attempt recovery regression separately failed and then passed (`red-recovery.log`, `green-recovery.log`). The final full suite is green.

Correct interrupted changes were retained. The incomplete doctor precheck was replaced by safe reporting; incomplete synthetic and import guards were strengthened. Two older routing tests were changed to use locally stubbed live-provider classifications on an HTTP source, because mock evidence is now deliberately barred from proposing real follow-up work. The existing retry test now supplies verified zero usage; missing usage has its own recovery-only tests. No historic approvals, dismissals or contact records were deleted.

## Provider contract and money controls

Official documentation was retrieved without credentials during repair: [TypeSafe API reference](https://docs.typesafe.ai/api) and [model reference](https://docs.typesafe.ai/models). They document `POST https://api.typesafe.ai/v1/systemone`, named Choice answers, the actual model and token usage. The pinned `jev-1.13.0` remains documented at $0.042 per million input tokens, with free output tokens. No price/model change was needed. Recheck these terms and account access before a future pilot; this retrieval did not verify an account or make an inference request.

The adapter retains fixed HTTPS, no redirects, no hidden retries, a bounded response and one paid-call gateway. Each send needs a persisted reservation and current collector lease. A reservation is 66,000 input tokens × 42 nano-USD = **$0.002772**. Under the current policy, reservations and settled costs count against the **$2 UTC-day allowance**. The estimate is not a provider tokenizer guarantee; reported input overruns pause admission and still record actual reported charges.

Malformed business answers create no judgments, while valid usage still settles money. Timeout/crash/missing usage never refunds a reservation automatically. A retry with known usage still consumes another reservation and preserves Retry-After/backoff. Recovery of an uncertain attempt without billing marks it `recovered`, retaining the reservation; later verified billing can reconcile that same row without disturbing newer work. The three-attempt ceiling is per evaluation and counts that evaluation's historical admissions; global attempt history does not stop other affordable work.

The legacy cumulative wallet and attempt-ceiling columns remain unchanged for compatibility and audit history, but no longer gate admission. Live mode, a positive UTC-day dollar allowance, price confirmation, token mode and credentials remain separate gates.

Doctor and the UI distinguish missing key, mode off, unconfirmed price, unconfirmed token mode, paused state, inconsistent ledger, active dispatch owner, insufficient current-day full-reservation allowance and cooldown. Doctor checks configuration/ledger only, never provider access or a particular evaluation's eligibility.

## Synthetic calibration and evidence compatibility

`jev seed-synthetic` prepares fixed fictional company/engineer evidence only. It creates no campaign, pilot, crawl job, accepted lead or provider attempt. Repeated immediate seeding reuses immutable document/evaluation identities; mock and live provider caches remain separate. Reuse events and latest checks are retained, and a later cache window/retention period can intentionally produce a new evaluation/evidence version. The source uses the offline collector and a fictional reserved domain; it is not permission for a real website.

Synthetic evidence has `provenance=synthetic-fixture`, a null retrieval timestamp, fixed content hash and parser/extraction identity. `retrieved_at` was already nullable in the original model and migration, so no timestamp migration was necessary. The console explicitly labels provenance and absence of retrieval. Even an actual live-provider answer to this fictional evidence cannot authorize follow-up work. Company-domain verification, existing campaigns, cached/reused evaluations and later source edits do not remove that boundary. The existing end-to-end demo can still route its own fixed offline pages.

Company candidates and engineers remain in the evidence pipeline before sales-contact filtering. No model result promotes an engineer into a sales lead, confirms deliverability, rewrites affiliation evidence, attaches a shared channel to a person or overrides suppression/review notes. Existing regression tests and process smokes cover these boundaries along with source scope, DNS/redirect protections, robots, throttling, recipe validation and canaries.

## Discovery capacity and the research shortlist

**No ordinary scheduler defect was found.** `register()` looks for an existing URL before checking inventory capacity, and `start()` requeues remembered approved URLs. `CapacityTests` proves that an inventory of ten URLs at a cap of ten still queues ten eligible remembered pages while storing no new URL. A paused automatic source at the same cap queues zero pages and keeps its pause state. A regular source's schedule being inactive does not by itself disable discovery: its campaign and collection/setup gates govern that work.

The separately confirmed company-router cap bypass is fixed. All candidate-creation paths now serialize on the campaign and preserve its cap. `smoke_jev.py` races two independent imports against one SQLite slot: exactly one saves metadata, the other reports `inventory_full`, and neither approves a source or creates a job.

The task's reported 50/50 inventory, `no_matching_cards` pause and zero real contacts are not re-verified production observations. The cap message explains rejected **new storage**, not why a known approved page cannot run. The bounded operational remedy after separate authorization is to inspect current URL decisions, source membership/scope, policy authorization, setup state, scores/exclusions and run state. Keep Host Merchant paused until authorized saved evidence supports a recipe that passes local validation and canary; a page with zero supported contacts is valid. Do not repeatedly resume it to manufacture activity.

For useful additional sources, retain the existing inventory/history and prepare a separate inactive manual-review campaign with a small explicit cap (for example ten metadata slots and five pages per run), or deliberately approve a bounded inventory change after review. Neither step is performed here. No automatic quota increases, dismissal revival, source approval or search/discovery run is part of import.

The supplied research file is a report wrapper, not an importer array. Its five candidate URLs/company labels were projected into `test-results/repair/shortlist-metadata.json` and validated offline; research assertions were replaced with a neutral unresolved-permission note. The original wrapper was first correctly rejected by the strict importer shape. The prepared file is ignored, not committed, imported, approved or fetched. Public DNS and robots permission remain unresolved.

Importer contract:

- Explicit existing campaign; dry-run by default, `--apply` required to write.
- Read at most 16 KiB + 1 byte before JSON parsing; accept 1–10 metadata objects and bounded URL/label/context fields.
- Reject raw credentials, controls (including encoded controls), unsafe schemes, nonstandard ports and literal private/local IPv4/IPv6 before saving. Never resolve hostnames during import; authorized transport still checks public DNS at execution.
- Dedupe normalized exact URLs and enforce transactional capacity. Keep existing approved/dismissed state and revoked source approvals; existing pending entries gain the manual-review requirement even at capacity.
- Honor campaign exclusions, policy denied domains and origin dismissals. New rows stay pending manual review under enabled site policies. No Source approval, queueing or onboarding side effects.
- Report row numbers/outcomes without echoing URL queries, contact values, labels or provider errors.

Future explicit operator commands, **not executed against production**:

```bash
.venv/bin/python manage.py import_discovery_candidates \
  --campaign CAMPAIGN_ID --file /absolute/path/to/shortlist-metadata.json --dry-run
# Only after reviewing the preview and authorizing the metadata write:
.venv/bin/python manage.py import_discovery_candidates \
  --campaign CAMPAIGN_ID --file /absolute/path/to/shortlist-metadata.json --apply
```

Replace `CAMPAIGN_ID` with the deliberately selected inactive review campaign. A successful import still requires existing manual source review before any collection. Do not pass the research-report wrapper directly.

## Fresh verification

Environment: Linux, checkout-owned `.venv`, **Python 3.14.2**, Django **5.2.17**. Python 3.12 remains the project's reference environment; it was not separately exercised here. No dependency manifests changed; AutoScraper is absent from the application environment. Extraction packs remain opt-in. Browser/Ollama/PostgreSQL/live-provider integrations were not run.

`test-results/repair/run.py` is a local verification wrapper: it removes inherited data/database/provider/Jev settings, explicitly overrides dotenv-sensitive live gates and credentials with safe values, chooses fresh workspace-local data/scratch paths and invokes this checkout's Python. After setup, an inherited socket guard rejects non-loopback connections. Smoke scripts create their own fresh databases and use free loopback ports; only their own child processes are terminated. Dependency installation and official public documentation were the permitted external operations. No production key was read, printed, requested or sent.

| Required command (invoked through the isolated wrapper) | Actual result / local log |
| --- | --- |
| `python3 setup.py --no-user` | Exit 0; install and fresh migrations succeeded; rerun applied 0005. `setup.log`, `setup-final.log` |
| `.venv/bin/python -m pip check` | Exit 0; no broken requirements. `pip-check.log` |
| `.venv/bin/python manage.py check` | Exit 0; no issues. `django-check.log` |
| `.venv/bin/python manage.py makemigrations --check --dry-run` | Exit 0; no changes detected. `migration-check.log` |
| `.venv/bin/python manage.py test` | **257 tests passed**, final run. `full-tests-final.log` |
| `.venv/bin/python scripts/smoke_local.py` | Exit 0; web/worker, login, evidence, review/suppression/export, discovery, automation/canary and restart passed. `smoke-local.log` |
| `.venv/bin/python scripts/smoke_jev.py` | Exit 0; real local web/worker, CSRF, restart, separate-process SQLite admission and concurrent import bounds passed. Repeated after recovery fix. `smoke-jev-final.log` |
| `.venv/bin/python scripts/smoke_extraction.py` | Exit 0; five layouts passed probe/validation/canary, ten observations, mock evidence and restart preservation. `smoke-extraction.log` |
| `.venv/bin/python manage.py benchmark_extraction --assert-fixtures` | Exit 0; 10/10 expected held-out person/email pairs, zero extras; fixture comparison only. `benchmark.log` |
| `.venv/bin/python manage.py jev doctor` | Exit 0; safely **not ready**: mode off, key absent, price and token mode unconfirmed; consistent ledger, zero attempts/spend/reservations. `doctor.log` |

All logs are under `test-results/repair/` and excluded from Git. `final-source.sha256` records the exact final changed/new file contents, including this handoff, for local packaging verification. Intentional RED runs are retained, not unresolved final failures. Tests emit expected injected fetch/parser failures and the pre-collection staticfiles warning; neither is a failing check. Optional AutoScraper comparison was not installed/run; the required application benchmark was.

A separate agent performed an **independent static review** of authorization, money, synthetic isolation, input/cap concurrency and additive migration safety, then reviewed the repaired diff and final recovery fix. That review found the old-attempt reconciliation defect, which was reproduced and fixed. Final review also checked the origin-dismissal guards: no remaining blocking findings. Test execution and final integration/self-review were performed by the implementation agent; the reviewer did not independently run the suite. No claim is made about real model accuracy, billing, site yield, PostgreSQL contention or production stability.

## Current daily-dollar live policy

The earlier one-cent / three-attempt calibration described in prior revisions is superseded. Paid admission now has **no daily or cumulative attempt-count ceiling** and does not use the legacy cumulative allowance as a stop. Lifetime attempt and spend fields remain audit history.

Live work is governed by `JEV_DAILY_ALLOWANCE_USD`, measured per UTC day as settled cost plus unresolved reservations. The current authorized runtime value is `$2`. Admission reserves the full conservative request amount before network I/O, remains single-flight, and keeps the one-second minimum pacing and three-attempt per-evaluation retry ceiling.

```dotenv
JEV_MODE=live
JEV_CAPTURE_ENABLED=1
JEV_LAYERED_BLOCKS_ENABLED=1
JEV_ROUTING_ENABLED=0
JEV_PRICE_CONFIRMED=1
JEV_ALLOW_ESTIMATED_TOKENS=1
JEV_DAILY_ALLOWANCE_USD=2
EXTRACTION_PACKS_ENABLED=0
BRAVE_SEARCH_ENABLED=0
```

Use `jev doctor` and `jev report` to verify the effective daily allowance, current-day exposure, unresolved reservation state and ledger consistency. There is no `jev budget` command and no `--attempt-limit` option. A missing or zero daily allowance fails closed. Pause/resume never erases charges, reservations, cooldowns, retry history or evidence checks.

```bash
.venv/bin/python manage.py jev doctor
.venv/bin/python manage.py jev report
.venv/bin/python manage.py jev pause --reason "Review live results"
.venv/bin/python manage.py jev resume --reason "Evidence and account state reviewed"
```

A timeout or unknown usage requires provider-account investigation and explicit recovery, not blind retries. Model success does not authorize discovery, source approval, routing, contact promotion or recipe changes.

## Upgrade checklist — requires separate go-ahead

1. Package the reviewed dirty source/tests/migration/docs into a local revision and record its exact hash and checksum. Preserve the operator's existing live untracked integration plan and any live code changes. Do not rebase this build onto main, overwrite the live tree or deploy the base hash alone.
2. Record the actual deployed commit, launcher/service definition, environment/data locations, database engine, service user, private bind and active processes. These details were not inspected during repair. Stop the existing `clearpay-lead-engine.service` / owning launcher and confirm both web and worker are stopped; do not start a second collector.
3. Make a private, complete stopped-state backup of the actual SQLite data directory including database, WAL and SHM files together; also preserve `.env`, service definition, accounts, migrations, review notes, suppression, source approvals, campaigns, recipes, Jev ledger and pending job state. Do not copy only the main SQLite file while writers may exist. Use the appropriate consistent backup if PostgreSQL is actually configured. Keep backups outside Git.
4. Retain paid/capture/routing/search gates off. Keep real campaigns/source scheduling paused until a separately authorized operational review; preserve all counters and history. Do not replace production data with this repair's empty database, synthetic demo or virtualenv.
5. Install the reviewed build with the intended Python environment and run `python3 setup.py --no-user`, `pip check`, Django checks and migration-drift checks against the deliberately configured data path. **Migration 0005 adds only nullable `JevControl.cumulative_attempt_limit`; no destructive backfill or data reset.** All earlier additive classification/discovery/automation migrations must also apply when upgrading from the older live branch. Verify migrated accounts and review/suppression state before allowing collection.
6. Keep the previously supplied private bind `100.81.228.123:8017` and existing private access controls after confirming the actual service configuration. Do not change to a public bind. Preserve trusted-host/CSRF and service configuration deliberately rather than accepting launcher defaults.
7. Start the existing owning service once, with exactly one collector worker. Verify service health, private listener, sign-in, existing leads/reviews/suppression, queues, source/policy pauses and recipe versions. Run doctor (no provider request), confirm unchanged spend/reservations and inspect any uncertain owners. Observe stable lease/heartbeat and restart behavior before a separately approved workload.
8. Installed/offline-tested does not mean credential-configured, live-provider-verified, live-discovery-enabled or production-stable. Reconfirm each state separately; stop on migration/accounting/authorization errors rather than resuming queues blindly.

That original repair performed no production upgrade, service restart, key configuration, paid test or real discovery. Current runtime status must be established from `jev report`, process state and the live database rather than this historical sentence.

## Rollback and recovery

For a failed deployment, stop both new processes first. The reliable rollback is the pre-upgrade complete database/config/service/code backup; retaining the upgraded database separately preserves evidence collected after that backup for deliberate reconciliation. Restore database/WAL/SHM as a consistent set, never mix versions or overwrite a running writer. Do not reset an uncertain Jev ledger or assume a timeout was free.

For code rollback while keeping the upgraded database, use the current build first to disable Jev paid/capture/routing work and company pilots, pause affected campaigns and regular schedules, and revoke approvals on automatic sources. Older code does not enforce these automatic setup/provenance gates or the current UTC-day money policy. **Never run older code against enabled automatic/company job modes**, even if additive columns are technically readable. Keep those modes disabled until the matching newer code is restored. Avoid destructive reverse migrations and retain prior recipe versions, evidence, review state and ledger history.

For an uncertain attempt, first stop/verify the prior worker and transport, inspect the recorded attempt and account billing privately, then use an explicitly authorized recovery:

```bash
.venv/bin/python manage.py jev recover ATTEMPT_UUID --worker-stopped \
  --reason "Old worker stopped; billing unknown; explicit recovery authorized"
# Only after independently verifying the actual charge:
.venv/bin/python manage.py jev recover ATTEMPT_UUID --billed-nusd VERIFIED_INTEGER_NANO_USD \
  --reason "Specific attempt billing independently verified"
```

The first command keeps the full reservation and may make that evaluation eligible for another attempt; therefore keep Jev paused until retry is authorized. The second reconciles only that attempt and cannot change a newer in-flight evaluation. Never supply zero without evidence of zero charge. Recovery preserves lifetime attempt history, cooldown and evaluation backoff. Inspect `jev report` / `jev doctor` before resuming any permitted work.
