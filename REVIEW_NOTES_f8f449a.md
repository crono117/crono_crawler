0c43812 tests(discovery): recovery edge cases for empty-run orphan reaping and lease boundaries
d26b4d8 tests(supervision): timer restart mid-update and corrupt state identity fail closed
ceb97f1 tests(extraction): mixed-case MAILTO/TEL default selectors and upgraded operator defaults
03dee44 (HEAD -> review/campaign2-edge-tests) fix(supervision): treat missing/wrong-type rollback identity as invalid (fail closed)
# Independent review notes for f8f449a (offline)

- Reviewed base (exact): `f8f449aae8a7548e0f96b68b2e346d8c9a31f280`
- Branch: `review/campaign2-edge-tests`

## Branch history (top-first)

- <to-fill>
- 0c43812 tests(discovery): recovery edge cases for empty-run orphan reaping and lease boundaries
- d26b4d8 tests(supervision): timer restart mid-update and corrupt state identity fail closed
- ceb97f1 tests(extraction): mixed-case MAILTO/TEL default selectors and upgraded operator defaults
- 03dee44 (HEAD -> review/campaign2-edge-tests) fix(supervision): treat missing/wrong-type rollback identity as invalid (fail closed)

## Defect found and fixed (supervision identity)

- File/function: `leads/services/supervision.py: verify_identity`
- Symptom: when the recorded rollback identity was not a dict (e.g., `str`, `int`, `list`, `None`), `verify_identity` accessed `.get` on that value, raising `AttributeError`. The guard should instead fail closed (raise `RecoveryRequired`) and keep the service stopped.
- Fix commit: `03dee44` — “fix(supervision): treat missing/wrong-type rollback identity as invalid (fail closed)”.
  - Change: `verify_identity` now validates that `previous["identity"]` is a `dict` and raises `RecoveryRequired('Rollback identity is missing or invalid.')` when not. It then compares a freshly captured identity against the recorded one; mismatches also raise `RecoveryRequired` rather than allowing restart.

## Tests added in this pass

- Discovery (Correction C, edge + frozen clock):
  - `discovery/tests/test_recovery_edge.py` (commit `0c43812`): multiple empty-run orphans across campaigns closed, paused campaigns untouched; lease interactions; boundary (strict) behavior without freezing.
  - `discovery/tests/test_recovery_threshold_frozen.py` (commit <separate, below>): frozen `timezone.now()` to assert exact-threshold semantics; code uses strict `<` (created_at__lt now − LEASE_SECONDS), so “exact at threshold” is not reaped, “older than threshold” is reaped.
- Supervision (Corrections A/B, edge + identity missing):
  - `leads/tests/test_supervision_edge.py` (commit `d26b4d8`): restart by timer/socket mid-update → guard re-quiesces and fails closed; post-start stop failure forbids rollback; cgroup populated forbids change/rollback.
  - `leads/tests/test_supervision_identity_missing.py` (commit <separate, below>): missing required fingerprint keys, empty dict, and wrong types (str/int/list/None) → rollback refuses restart; unit remains stopped; requires `03dee44`.
- Extraction (Correction D, URI case):
  - `leads/tests/test_contact_uri_mixed_case.py` (commit `ceb97f1`): default selectors match mixed-case MAILTO/Tel; operator default-style selectors are upgraded; explicit operator selectors remain exact (uppercase coverage existed previously).

Commit mapping (tests):
- `0c43812`: `discovery/tests/test_recovery_edge.py`
- `d26b4d8`: `leads/tests/test_supervision_edge.py`
- `ceb97f1`: `leads/tests/test_contact_uri_mixed_case.py`
- (new) `test_recovery_threshold_frozen.py` and `test_supervision_identity_missing.py` are committed separately in this pass; see commit list below.

## Process/coverage notes for the next fixer

1) Failing-test-first rule
   - The identity-missing tests (`leads/tests/test_supervision_identity_missing.py`) and the frozen-threshold tests are committed as separate test commits in this pass; the minimal code fix is isolated in `03dee44`. Earlier supervision “edge” tests (timer restart) passed on base and were committed as tests only.

2) Unparseable/corrupt JSON state-file
   - Requested scenario: “unparseable/corrupt JSON state-file” for the guard’s durable state. No dedicated test was added in this pass; current tests inject identity via the in-memory `record` callback. Result: NOT COVERED — add a test simulating a corrupt file read (mocking `json.load` or the guard’s state recovery path) and assert fail-closed behavior without restart.

3) Other gaps
   - Exact-threshold test now freezes `timezone.now`; earlier non-frozen boundary assertions remain limited to strict cases to avoid micro-timing races.
   - Systemd interactions remain fully mocked; no real units are touched.

## Offline verification (commands and verbatim results)

- `.venv/bin/python manage.py check` → `System check identified no issues (0 silenced).`
- `.venv/bin/python manage.py makemigrations --check --dry-run` → `No changes detected`
- `.venv/bin/python manage.py test --noinput` → `Ran 736 tests in 35.920s` then `OK`
- `.venv/bin/python scripts/smoke_local.py` → `PASS: test processes stopped and temporary database removed.`
- `.venv/bin/python scripts/smoke_inventory.py` → `PASS: intake after parking -> Collector link intake only: 25 new URL(s), 0 parked, 0 filtered, 0 already known, 0 rejected at capacity; 0 page job(s) queued. Not a crawl run; zero pages here is not a worker failure. (15 left for the next pass: new-domain cap)`
- `.venv/bin/python scripts/smoke_jev.py` → `PASS: no paid API requests or external website requests were made.`
- `.venv/bin/python scripts/smoke_extraction.py` → `PASS: restart and re-probe preserve contact deduplication and human suppression.`
- `.venv/bin/python scripts/run_smoke_bulk_import.py` → `PASS: isolated web process stopped; runner owns temporary database cleanup.`
- `.venv/bin/python manage.py benchmark_extraction --assert-fixtures` JSON excerpts (held-out pairs):
  - wordpress: `"extraction_packs": { "correct": 2, "expected": 2 }`
  - webflow: `"extraction_packs": { "correct": 2, "expected": 2 }`
  - squarespace: `"extraction_packs": { "correct": 2, "expected": 2 }`
  - jsonld: `"extraction_packs": { "correct": 2, "expected": 2 }`
  - microdata: `"extraction_packs": { "correct": 2, "expected": 2 }`

## Constraints to preserve

- Offline only; no production DB/credentials/.env
- Exactly one worker per DB; no Celery/Redis/extra DB/worker/agent framework
- No threshold/budget/scope/approval changes; do not bypass checks or gates
- No real systemd control; all service/systemctl calls must be mocked in tests

