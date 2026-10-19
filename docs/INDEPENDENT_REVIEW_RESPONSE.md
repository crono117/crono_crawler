# Response to independent review of 247b7ca

The independent review in `docs/reviews/2026-10-19-independent-review-247b7ca.md`
**rejects deployment and merge** of 247b7ca. This response documents local amendments,
not independent approval. The review's baseline and artifact hash remain unchanged in the
copied original. Exact amended HEAD is supplied by the package manifest / git rev-parse HEAD;
all changes are descendants of 247b7ca on fix/campaign2-recovery-and-yield. Nothing was pushed.

## Findings and changes

| Review finding | Correction / evidence | Status |
|---|---|---|
| Blocker 1: rollback beneath a live writer | `leads/services/supervision.py:quiesce,require_quiescent,guarded_update` issues stop and verifies inactive, MainPID=0, ControlPID=0, KillMode=control-group, and empty/removed cgroup before change and EVERY rollback. Post-start readback failure must pass a fresh stop gate before rollback. Failed stop forbids callbacks and restart. | Fixed locally; RED→GREEN regressions; re-review required |
| High 2: orphan closed but campaign not due | `leads/services/worker.py:acquire_lease` locks affected campaigns, closes only old active-campaign empty queued/running runs, and advances only their future next_due_at to now in the same transaction. The ordinary scheduler creates replacement work through normal gates. | Fixed; 30-day future schedule regression proves replacement queued work under one lease |
| High 3: uppercase explicit selectors regress | `leads/services/extraction.py:soup_for,selected_text,DEFAULT_SELECTORS` preserves original DOM. New defaults use CSS case-insensitive scheme matching; only exact historical lowercase default/generated selectors are upgraded at field selection. Arbitrary explicit operator selectors, including uppercase and empty selectors, remain intact. Version 0.4.2 changes cache signatures. | Fixed; RED→GREEN original-DOM, explicit MAILTO/TEL and association fixtures |
| High 4: rollback identity unverified | `capture_identity,verify_identity` records clean Git SHAs and hashes of unit/drop-ins, explicit effective unit configuration, EnvironmentFiles, interpreter/script, checkout .env and caller-declared extra config files. Zero-exit/no-op rollback cannot restart if fingerprints differ. Unknown wrappers, dirty/uninspectable checkouts and pending daemon-reload fail preflight. No raw configuration/environment/argv secrets enter the state file. | Fixed locally; no-op/partial/revision/dirty/config/interpreter tests; re-review required |
| High 5: all-writer outage needed | `docs/SYSTEMD_RECOVERY.md` explicitly requires a separately reviewed inventory and quiescence of ALL database writers and triggers, schema-compatible aligned checkouts, fresh Backup API copy, and private rollback plan. The helper is one-unit only. Separate synthetic Jev soak is not repointed. | Mandatory release gate; not performed on a host |
| Medium: dirty/uninspected status | `checkout_info,status,snapshot` reports dirty_checkout and uninspected_checkout and never marks a shell wrapper current. Only supported direct .venv Python entry points are inspected. | Fixed with real throwaway Git-repository fixtures |
| Medium: directory durability | `scripts/guard_service_update.py:write_state` fsyncs file and parent directory before stopping. Directory sync failure aborts preflight. | Fixed with filesystem/ordering tests |
| Medium: user network target not readiness | Recovery guide explicitly distinguishes ordering from network readiness and retains transport backoff and unit failure restart; verifies real HTTP/lease/queue progress after a future boot. | Documented; no real host/network/reboot validation |
| Medium: malformed HTTP ports | `crawler_status` validates URL/port before opening a connection, converts ValueError to CommandError, rejects non-numeric/out-of-range/zero ports and malformed bracket URLs. | Fixed with no-connection regression |
| Medium: structured duplicate count | Structured extractor maps duplicate_card to diagnostics.duplicates; final de-dup adds rather than overwrites prior counts. | Fixed with repeated JSON-LD Person fixture |
| Medium: SQLite rebuild cost | Fresh synthetic SQLite Backup API copy rehearsed 0010→current→0010→current, checks integrity/backfill/retention and unchanged source digest. | Synthetic rehearsal passed; production-size timing/peak space still required |

## Guard scope and limits

This deliberately recognizes only direct .venv/bin/python followed by absolute run-local.py
or manage.py (optional -u). Ambiguous systemd escaping/wrappers are rejected, not guessed.
Guarded units must use KillMode=control-group and be in stable active/inactive, enabled/disabled
states. PID/cgroup checks and systemd interactions are mocked in tests. No installed unit was
inspected; unsupported cgroup state must fail rather than bypass verification.

Fingerprinting covers the declared code/unit/config files and effective explicit unit settings,
not the entire host, inherited manager environment, database, installed package set or undeclared
settings. The release owner must inventory additional dependencies and use --identity-file when
appropriate. One reviewed change owner and a coordinated all-writer outage are required; the
helper cannot fence concurrent administrator changes or unrelated manual writers.

A rollback callback must restore the old code, schema compatibility and unit/configuration,
reload systemd when necessary, and leave the service stopped. The guard verifies declared
identity before a previously active unit restarts. If stop/rollback/identity checks fail, it
makes a best-effort stop, preserves prior enablement, never restarts and reports operator recovery
required. A SIGKILL/power loss can interrupt the process; fsynced state supports manual recovery
but does not make rollback transactional across the OS and database. Do not reboot/start an
enabled unit left in an unverified configuration.

## Verification

All checks used a clean inherited environment, random secret, temporary DATA_DIR and SQLite,
ACTIVITY_LOG=off, paid/search/index integration disabled, mock providers and fictional fixtures.
An external temporary socket guard blocked non-loopback connections; smokes used localhost only.
No production settings, credentials, real target HTML or installed systemd units were used.

- New RED regressions reproduced the orphan's 30-day delay, explicit uppercase selector loss,
  rollback beneath a live service, failure to verify stop/PIDs, and no-op rollback restart.
- Focused guard/identity/durability/structured diagnostics run: **21 tests passed**; additional
  extraction/recovery run: **9 tests passed**; diagnostic/guard run: **25 tests passed**.
- Full suite: **725 tests passed in 72.237 seconds**, exit 0.
- Django check: no issues. makemigrations --check --dry-run: no changes detected.
- smoke_local.py, smoke_inventory.py, smoke_jev.py, smoke_extraction.py,
  run_smoke_bulk_import.py: **all PASS, exit 0**. Bulk smoke only through its isolating runner.
- benchmark_extraction --assert-fixtures: **10/10 expected pairs, zero extras**, exit 0.
- Historical-model writer check: pre-0011 insert rejected by the upgraded schema, as expected;
  migration forward/backward/reapply retained rows and backfilled legacy reasons.
- Fresh **synthetic** SQLite Backup API copy: 500 run rows and 3,000 job rows retained;
  forward 1.268 s, reverse 0.286 s, reapply 0.216 s; source digest unchanged; integrity OK.
  Source file 1,273,856 bytes; final copy 2,052,096 bytes; scratch free space 29,144,064,000
  bytes at measurement. Peak table-rebuild usage NOT measured. These are fixture mechanics,
  not production scale/downtime/headroom claims. No production backup was supplied or accessed.
- git diff --check: clean. No new migration added; 0011's compatibility boundary is unchanged.

## Remaining gates

1. Independent review of the amended exact commit. This author's response does not overrule
   the original rejection or certify readiness for merge/deployment.
2. If publishing is explicitly authorized, push only this review branch; verify the exact remote
   SHA, then rerun tests from that exact pushed checkout. No publishing is authorized here.
3. Separately authorize host readback and a fresh production Backup API copy; rehearse migration,
   reversal/reapplication, retained decisions/evidence and real timing/peak disk on that copy.
4. Review an all-writer deployment/rollback handoff and separately authorize production deployment,
   service re-enablement, linger and reboot verification. Preserve budgets, thresholds, approvals,
   dismissals, campaign limits and separate synthetic Jev soak configuration.

Production, credentials, paid API requests, target-site requests, remote branches and actual
service/deployment state were not accessed or changed by this amendment. The only review input
was the attached Markdown report; no independent-review code was executed.
