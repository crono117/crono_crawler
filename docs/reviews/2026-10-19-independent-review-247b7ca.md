# Independent review — Campaign 2 recovery branch

Date: 2026-10-19

## Scope

- Repository artifact: `crono-crawler-review-247b7ca.zip`
- Archive SHA-256: `c97a3d19a18a101afaa3c0c808e1c4434a05a18105685e508b8499f5d99ae76f`
- Baseline: `10f37e8538e634ad46b000a8db49f5475e65e4d2`
- Reviewed HEAD: `247b7ca75d586a65c0d9bd832d718058853a8c17`
- Branch in isolated review clone: `review/campaign2-recovery-and-yield`
- Production, credentials, external providers, target sites, installed systemd units, and remote Git were not accessed or modified.

## Verdict

**REJECT for deployment and merge in the current form.**

The implementation passes its test suite, but independent review found one deployment-safety blocker and multiple high-severity correctness/recovery defects. The branch should be amended and reviewed again before publishing or deployment.

## Blocker

### 1. Rollback can run while the service is still active

`leads/services/supervision.py:79-92` puts the initial stop, change, and state restoration in one `try`. Any exception invokes `rollback()` without first stopping and verifying the service.

This includes a failure after `restore()` has already issued `start`. The reproduced event order was:

```text
record → stop → change → enable → start → rollback → enable → start
```

There is no stop between the first start and rollback. A rollback script could therefore change checkout, unit configuration, or schema beneath a live writer.

Required correction:

- Before every rollback, issue stop and verify `ActiveState=inactive` (and the appropriate service PID/cgroup gates).
- If quiescence cannot be proven, do not run rollback; leave the unit stopped/failed as far as possible and require operator recovery.
- After rollback, verify the previous checkout/config identity before restarting it.

## High severity

### 2. Empty-run recovery can idle an active campaign until its normal interval

`leads/services/worker.py:103-109` completes an old empty run but does not make the campaign due or create replacement work. `discovery/services.py:617-626` may already have moved `next_due_at` far into the future, while `discovery/services.py:750-758` only starts replacement work when that timestamp is due.

Independent reproduction with `next_due_at` 30 days ahead produced:

```text
closed_status=completed
open_runs=0
next_due_at=2026-11-18T20:35:49.813896+00:00
```

This contradicts the recovery goal: the orphan is closed, but work does not resume.

Required correction:

- Atomically mark affected campaigns due immediately, or safely create/schedule replacement work after closing an orphan.
- Preserve paused campaigns and all existing jobs/deferrals.
- Add a regression test proving the active campaign gains resumable work rather than merely losing the orphan.

### 3. Uppercase operator contact selectors regress after URI normalization

`leads/services/extraction.py:166-180` rewrites `MAILTO:`/`TEL:` schemes to lowercase before recipe selectors run. Explicit operator selectors are then applied at `leads/services/extraction.py:405-407`; unlike generated/default selectors, arbitrary explicit selectors do not receive fallback at `leads/services/extraction.py:311-316`.

Independent reproduction:

```text
selector a[href^="MAILTO:"]: 0 matches
selector a[href^="mailto:"]: 1 match
```

An existing uppercase operator recipe can therefore produce zero records after the extraction-version bump forces re-extraction. That can mark previously observed contacts absent.

Required correction:

- Preserve compatibility with existing case-specific operator selectors while supporting mixed-case URI schemes.
- Add regression tests for explicit uppercase `MAILTO:` and `TEL:` recipes, not only default/generated lowercase selectors.

### 4. Rollback success is trusted without verifying restored code/config identity

`leads/services/supervision.py:12-27` records state and paths, but not the prior Git revisions or unit/config hashes. After `rollback()` exits zero, `guarded_update()` can restart the unit without proving that the previous checkout and unit configuration were restored.

Required correction:

- Record the expected previous revision(s) and reviewed unit/config identity before stopping.
- Verify rollback results against those recorded values before permitting restart.
- Treat a no-op or partial rollback as failure and keep the unit stopped.

### 5. Migration 0011 requires a coordinated all-writer outage

The migration adds non-null columns and a terminal-state constraint that old writers do not satisfy. A Common Crawl process or other writer using pre-0011 code can fail after migration. The single-unit guard controls only its named unit.

Required release gate:

- Enumerate and quiesce every writer to the production database, including Common Crawl.
- Verify all writer checkouts are schema-compatible before any writer resumes.
- Do not rely on the one-unit guard to provide this coordination.

## Medium severity

- `revision()` checks only `git rev-parse HEAD`; a dirty checkout at the expected commit can be reported as current. ExecStart wrappers or unrecognized entry points can also escape revision inspection (`leads/services/supervision.py:19-24,48-67`).
- The durable state file is fsynced, but its parent directory is not (`scripts/guard_service_update.py:25-30`), weakening crash/power-loss durability.
- The user unit's `network-online.target` ordering does not by itself prove host networking is ready for a user manager (`deploy/systemd/crono-lead-engine.service.in:3-4`). The application must tolerate delayed networking.
- Invalid/non-numeric HTTP probe ports can raise an uncaught `ValueError` instead of `CommandError` (`leads/management/commands/crawler_status.py:39-49`).
- Structured rules-only extraction can put `duplicate_card` in rejection diagnostics while reporting the dedicated duplicate count as zero (`automation/recipes.py` and `discovery/diagnostics.py:21-29`).
- On SQLite, migration 0011 performs repeated table rebuilds. Rehearse timing and disk headroom on a fresh Backup API copy before production.

## Verification performed

- ZIP and every internal SHA-256 checksum: PASS.
- Bundle imported into a new isolated clone: PASS.
- Exact HEAD and clean worktree: PASS.
- `git diff --check`: PASS.
- Django system check: PASS.
- Migration drift check: PASS.
- Full suite: **708 tests passed**.
- All five requested offline smoke suites: PASS.
- Held-out extraction benchmark: **10/10, zero extras**.
- Independent migration rehearsal: `0010 → current → 0010 → current`: PASS; rows retained, legacy fields backfilled, constraint enforced.
- Focused independent reviewers: 24 recovery/diagnostic tests, 15 supervision/status tests, and 20 extraction/query tests passed.
- Rollback-live-writer defect: independently reproduced.
- Uppercase explicit-selector regression: independently reproduced.
- Empty-run rescheduling defect: independently reproduced.

The first combined verification shell reported exit 1 after producing successful output through the benchmark; rerunning its final benchmark and diff-check commands independently returned exit 0. All named checks above have direct successful output.

## Required next patch

1. Make rollback fail-closed around verified service quiescence.
2. Record and verify prior checkout/unit identity before restart.
3. Reschedule or replace recovered empty runs immediately and safely.
4. Preserve explicit uppercase contact selector compatibility.
5. Add focused RED→GREEN tests for all four defects.
6. Improve dirty/uninspected checkout reporting and HTTP URL validation.
7. Rerun the full offline suite, smokes, benchmark, and migration rehearsal.
8. Obtain a new independent review of the amended exact commit.

No push, PR, merge, service operation, or production change is authorized by this report.
