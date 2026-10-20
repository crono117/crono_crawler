# Independent review notes for f8f449a (offline)

- Base: `f8f449aae8a7548e0f96b68b2e346d8c9a31f280`.
- Source: `review/campaign2-edge-tests`, pinned at `239387e5a0440751f9f92e7cb0ad84360003b969`; not rewritten or pushed.
- Destination: `fix/f8f449a-identity-fail-closed`, created from the exact base.
- History below is oldest first. The final documentation-only commit containing these notes follows the listed commits; its hash is available from `git rev-parse HEAD` (a commit cannot embed its own hash).

## Final implementation/test commit list

- `6dcbf046aa4ad5562d5f349ebff88958728f3e72` tests(discovery): recovery edge cases for empty-run orphan reaping and lease boundaries
- `1ce66b43bf2188e9be2f714f7dc154059efa7752` tests(supervision): timer restart mid-update and corrupt state identity fail closed
- `f899a386cfc267b39848c37a5a4a74eab30e9217` tests(extraction): mixed-case MAILTO/TEL default selectors and upgraded operator defaults
- `d33d275bcd351261008f28d584667480570f75fc` tests(discovery): freeze exact orphan recovery threshold
- `679d6fab307e5d74d7645ca9932b4b15fff8e698` tests(supervision): reject missing and invalid rollback identities before fix
- `c06c949a905547b907944abc460518c9dc3bd7ea` fix(supervision): treat missing/wrong-type rollback identity as invalid (fail closed)
- `dcfc8956bc9342cf7dd3efda6102e15a5c6b0aa9` tests(supervision): refuse rollback restart with corrupt on-disk state
- `2482c3f0e9e1f8702eaf8ec2a41478de6e28749f` fix(supervision): verify durable rollback state before restart

Publishing note: ordinary Git push could not authenticate in this environment. The
connected GitHub integration recreated the commits in the same order, and every
implementation/test Git tree SHA was checked against the locally tested cherry-pick
history before publication. This changes commit metadata/hashes, not file contents.
The failure transcripts below came from those local equivalents. Final published
commit hashes are listed above. No force update or source-branch rewrite is used.

## Test files by commit

| New commit | Source | Test file |
| --- | --- | --- |
| `6dcbf04` | cherry-pick `0c43812` | `discovery/tests/test_recovery_edge.py` |
| `1ce66b4` | cherry-pick `d26b4d8` | `leads/tests/test_supervision_edge.py` |
| `f899a38` | cherry-pick `ceb97f1` | `leads/tests/test_contact_uri_mixed_case.py` |
| `d33d275` | split unchanged from `239387e` | `discovery/tests/test_recovery_threshold_frozen.py` |
| `679d6fa` | split unchanged from `239387e` | `leads/tests/test_supervision_identity_missing.py` |
| `dcfc895` | new regression | `leads/tests/test_supervision_state_file.py` |

`c06c949` is the cherry-pick of `03dee44`, changing only `verify_identity`.
`2482c3f` changes only `leads/services/supervision.py` and `scripts/guard_service_update.py`.

## Failing first: identity type

On the local equivalent of published `679d6fa`, before applying the original fix, ran:

```text
python manage.py test leads.tests.test_supervision_identity_missing --noinput
```

Exit status 1. All four wrong-type subcases raised AttributeError, rather than RecoveryRequired:

```text
AttributeError: 'str' object has no attribute 'get'
AttributeError: 'int' object has no attribute 'get'
AttributeError: 'list' object has no attribute 'get'
AttributeError: 'NoneType' object has no attribute 'get'

Ran 3 tests in 0.011s

FAILED (errors=4)
```

The unmodified source regression is committed BEFORE `c06c949` (`03dee44` cherry-pick).
After that fix, the same command exited 0:

```text
Ran 3 tests in 0.007s

OK
Found 3 test(s).
System check identified no issues (0 silenced).
```

The other four test modules (discovery edges, frozen threshold, supervision edges,
contact URI case) were run together before either fix, against base production code:

```text
Ran 8 tests in 0.042s

OK
Destroying test database for alias 'default'...
Found 8 test(s).
System check identified no issues (0 silenced).
```

## Failing first: corrupt durable JSON

Inspection found that the original CLI only wrote the state file; there was no automated
state read path. Its rollback verified the in-memory snapshot even if the durable file
was corrupted after it was written. The regression invokes the real CLI `main`, parser,
`write_state`, and guard; only snapshot/systemctl/capture_identity and script subprocesses
are mocked. Temporary executable fixture scripts are validated but never executed.
No JSON decoder or file reads are mocked.

On the local equivalent of published `dcfc895`, before the readback fix, ran:

```text
python manage.py test leads.tests.test_supervision_state_file --noinput
```

Exit status 1. The intact-record control passed; truncated JSON, non-JSON/invalid UTF-8
bytes, empty file, missing file, and four parseable invalid/changed records allowed restart:

```text
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

Ran 6 tests in 0.030s

FAILED (failures=8)
Found 6 test(s).
System check identified no issues (0 silenced).
```

Separate fix `2482c3f` adds `read_state` using real `json.load`, mapping unreadable,
undecodable, unparseable or non-object files to RecoveryRequired. The CLI supplies
this reader to `guarded_update`; after rollback and verified quiescence, the saved
record must equal the initial snapshot before the existing identity check/restart.
Mismatch also raises RecoveryRequired. Existing cleanup keeps the unit stopped and
preserves enablement. There is no recovery success return or success output.
The intact-state control still restores prior activity and propagates the deployment error.
The optional reader preserves existing in-memory library callers; this change does not add
a standalone crash-recovery command or change the documented manual recovery procedure.

Combined supervision checks after both fixes:

```text
Ran 31 tests in 0.287s

OK
Found 31 test(s).
System check identified no issues (0 silenced).
```

## Offline verification environment

Used a clean detached Git worktree at local `3263c9f4a6934ca5d2674c7644e05574389d2762`,
with tree identical to published `2482c3f0e9e1f8702eaf8ec2a41478de6e28749f`,
whose code/test tree is identical to the final branch (the following commit changes only
this Markdown file). Asserted no `.env`; generated a random DJANGO_SECRET_KEY; used
an empty temporary HOME and DATA_DIR with SQLite, no DATABASE_URL, JEV_MODE=off,
BRAVE_SEARCH_ENABLED=0, COMMON_CRAWL_ENABLED=0 and ACTIVITY_LOG=off. Environment
was explicitly constructed rather than inherited. Used the already-installed Python 3.12
virtualenv, Django 5.2.17; no dependency downloads. Smoke scripts create synthetic,
isolated databases/processes and use loopback. No external provider/target requests,
production access, production data/configuration, or real service control occurred.
Bulk import was invoked only via `scripts/run_smoke_bulk_import.py`.

## Final verification output (verbatim tails)

### `python manage.py check` — exit 0

```text
System check identified no issues (0 silenced).
```

### `python manage.py makemigrations --check --dry-run` — exit 0

```text
No changes detected
```

### `python manage.py test --noinput` — exit 0

```text
----------------------------------------------------------------------
Ran 742 tests in 43.973s

OK
Destroying test database for alias 'default'...
Found 742 test(s).
System check identified no issues (0 silenced).
Discovery demo run #1 queued. Start the worker and open Discovery. Expected: 3 fictional people and a new vendor URL awaiting review.
Discovery demo run #1 queued. Start the worker and open Discovery. Expected: 3 fictional people and a new vendor URL awaiting review.
```

### `python scripts/smoke_local.py` — exit 0

```text
PASS: actual web/worker startup, login, CSS, collection and service filtering.
PASS: published evidence, review persistence, CSV suppression, pause/run controls and deduplication.
PASS: full process restart resumes a queued job and retains the login session and review decisions.
PASS: discovery dashboard, link paths, sitemap-only page, three new fictional contacts and source review controls.
PASS: discovery queue restart, deduplication, URL dismissal and existing lead suppression all persist.
PASS: zero-contact worker diagnostics and recipe health alerts on all dashboard surfaces.
PASS: automatic recipe setup, local validation, canary, version history, controls and full process restart.
PASS: test processes stopped and temporary database removed.
```

### `python scripts/smoke_inventory.py` — exit 0

```text
PASS: intake after parking -> Collector link intake only: 25 new URL(s), 0 parked, 0 filtered, 0 already known, 0 rejected at capacity; 0 page job(s) queued. Not a crawl run; zero pages here is not a worker failure. (15 left for the next pass: new-domain cap)
```

### `python scripts/smoke_jev.py` — exit 0

```text
PASS: real web/worker demo, evidence UI, CSRF controls and restart persistence.
PASS: simultaneous process admission serialized; restart and crash recovery retain reservations.
PASS: simultaneous metadata imports respect inventory cap without approval or jobs.
PASS: no paid API requests or external website requests were made.
```

### `python scripts/smoke_extraction.py` — exit 0

```text
PASS: five platform layouts completed probe, validation and canary in a real worker.
PASS: 10 source observations, structured Jev evidence and mock judgments; no paid attempts.
PASS: restart and re-probe preserve contact deduplication and human suppression.
```

### `python scripts/run_smoke_bulk_import.py` — exit 0

```text
PASS: real authenticated HTTP upload → preview → confirm created only paused/unapproved sources.
PASS: preview no writes; repeat confirmation unchanged; invalid batch all-or-nothing; CSRF enforced.
PASS: example/schema/guide downloads; no collection/discovery jobs, candidates or paid attempts.
PASS: isolated web process stopped; runner owns temporary database cleanup.
```

### `python manage.py benchmark_extraction --assert-fixtures` — exit 0

```text
      "extraction_packs": {
        "correct": 2,
        "false_positive": 0,
        "missed": 0,
        "expected": 2,
        "recipe": {
          "engine": "structured-v1"
        }
      }
    }
  ]
}
```

Benchmark fixture summary (in addition to the verbatim tail above):

| Fixture | Correct / expected | False positives | Missed |
| --- | --- | --- | --- |
| wordpress | 2 / 2 | 0 | 0 |
| webflow | 2 / 2 | 0 | 0 |
| squarespace | 2 / 2 | 0 | 0 |
| jsonld | 2 / 2 | 0 | 0 |
| microdata | 2 / 2 | 0 | 0 |

## Remaining limits and constraints

No requested implementation or offline verification step remains unresolved. This is
mocked service control and synthetic test evidence, not installed-unit or production
validation. Production deployment/reboot, production-backup migration rehearsal,
writer inventory and the pre-existing coordinated release gates remain unperformed
and outside this task. No production configuration or database was read.

Only the destination branch is published using a non-force ref update. No PR,
merge, main update or source-branch rewrite. No threshold, budget, limit, approval,
suppression or scope changes. No additional service, database or agent framework.

## Complete failing-test transcripts

### identity-before (exit 1)

```text
..EEEE
======================================================================
ERROR: test_wrong_types_refuse_restart (leads.tests.test_supervision_identity_missing.GuardIdentityMissingTests.test_wrong_types_refuse_restart) (type='str')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 230, in guarded_update
    change()
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 17, in change
    raise ValueError("synthetic change failure")
ValueError: synthetic change failure

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 50, in test_wrong_types_refuse_restart
    self._run_guard_with_identity(identity)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 22, in _run_guard_with_identity
    supervision.guarded_update("fixture.service", change, lambda: fake.events.append("rollback"), record)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 247, in guarded_update
    verify_identity(state)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 157, in verify_identity
    actual = capture_identity(snapshot(previous['unit']), previous['identity'].get('extra_files', ()))
                                                          ^^^^^^^^^^^^^^^^^^^^^^^^
AttributeError: 'str' object has no attribute 'get'

======================================================================
ERROR: test_wrong_types_refuse_restart (leads.tests.test_supervision_identity_missing.GuardIdentityMissingTests.test_wrong_types_refuse_restart) (type='int')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 230, in guarded_update
    change()
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 17, in change
    raise ValueError("synthetic change failure")
ValueError: synthetic change failure

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 50, in test_wrong_types_refuse_restart
    self._run_guard_with_identity(identity)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 22, in _run_guard_with_identity
    supervision.guarded_update("fixture.service", change, lambda: fake.events.append("rollback"), record)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 247, in guarded_update
    verify_identity(state)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 157, in verify_identity
    actual = capture_identity(snapshot(previous['unit']), previous['identity'].get('extra_files', ()))
                                                          ^^^^^^^^^^^^^^^^^^^^^^^^
AttributeError: 'int' object has no attribute 'get'

======================================================================
ERROR: test_wrong_types_refuse_restart (leads.tests.test_supervision_identity_missing.GuardIdentityMissingTests.test_wrong_types_refuse_restart) (type='list')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 230, in guarded_update
    change()
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 17, in change
    raise ValueError("synthetic change failure")
ValueError: synthetic change failure

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 50, in test_wrong_types_refuse_restart
    self._run_guard_with_identity(identity)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 22, in _run_guard_with_identity
    supervision.guarded_update("fixture.service", change, lambda: fake.events.append("rollback"), record)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 247, in guarded_update
    verify_identity(state)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 157, in verify_identity
    actual = capture_identity(snapshot(previous['unit']), previous['identity'].get('extra_files', ()))
                                                          ^^^^^^^^^^^^^^^^^^^^^^^^
AttributeError: 'list' object has no attribute 'get'

======================================================================
ERROR: test_wrong_types_refuse_restart (leads.tests.test_supervision_identity_missing.GuardIdentityMissingTests.test_wrong_types_refuse_restart) (type='NoneType')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 230, in guarded_update
    change()
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 17, in change
    raise ValueError("synthetic change failure")
ValueError: synthetic change failure

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 50, in test_wrong_types_refuse_restart
    self._run_guard_with_identity(identity)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_identity_missing.py", line 22, in _run_guard_with_identity
    supervision.guarded_update("fixture.service", change, lambda: fake.events.append("rollback"), record)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 247, in guarded_update
    verify_identity(state)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/services/supervision.py", line 157, in verify_identity
    actual = capture_identity(snapshot(previous['unit']), previous['identity'].get('extra_files', ()))
                                                          ^^^^^^^^^^^^^^^^^^^^^^^^
AttributeError: 'NoneType' object has no attribute 'get'

----------------------------------------------------------------------
Ran 3 tests in 0.011s

FAILED (errors=4)
Found 3 test(s).
System check identified no issues (0 silenced).
```

### state-before (exit 1)

```text
F.FFFFFFF
======================================================================
FAIL: test_empty_file_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_empty_file_refuses_restart)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 74, in test_empty_file_refuses_restart
    self.assert_refused(b'')
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_missing_file_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_missing_file_refuses_restart)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 77, in test_missing_file_refuses_restart
    self.assert_refused(remove=True)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_non_json_bytes_refuse_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_non_json_bytes_refuse_restart)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 71, in test_non_json_bytes_refuse_restart
    self.assert_refused(b'\xff\xfe\x00not json')
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_parseable_but_changed_record_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_parseable_but_changed_record_refuses_restart) (payload=b'null')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 82, in test_parseable_but_changed_record_refuses_restart
    self.assert_refused(payload)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_parseable_but_changed_record_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_parseable_but_changed_record_refuses_restart) (payload=b'[]')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 82, in test_parseable_but_changed_record_refuses_restart
    self.assert_refused(payload)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_parseable_but_changed_record_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_parseable_but_changed_record_refuses_restart) (payload=b'{}')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 82, in test_parseable_but_changed_record_refuses_restart
    self.assert_refused(payload)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_parseable_but_changed_record_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_parseable_but_changed_record_refuses_restart) (payload=b'{"unit": "other.service"}')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 82, in test_parseable_but_changed_record_refuses_restart
    self.assert_refused(payload)
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

======================================================================
FAIL: test_truncated_json_refuses_restart (leads.tests.test_supervision_state_file.GuardStateFileTests.test_truncated_json_refuses_restart)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 68, in test_truncated_json_refuses_restart
    self.assert_refused(b'{"unit": "fixture.service", "identity":')
  File "/workspace/scratch/8a92eaa0b7dd/crono_crawler/leads/tests/test_supervision_state_file.py", line 61, in assert_refused
    self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
AssertionError: 'active' != 'inactive'
- active
+ inactive
? ++
 : unreadable rollback record allowed restart

----------------------------------------------------------------------
Ran 6 tests in 0.030s

FAILED (failures=8)
Found 6 test(s).
System check identified no issues (0 silenced).
```
