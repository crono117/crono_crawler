# Bulk source JSON upload — verified candidate handoff

## Status and scope

Implemented in the isolated checkout `/home/crono/dev/crono_crawler-bulk-import`, branch `feat/bulk-source-json`, based on deployed revision `a6ecef14f0d8d314212dae0a7c7e5a1093b83448`. This document does **not** claim deployment. No production service restart, database import, activation, remote push or paid request was performed for this feature.

The feature adds a Sources → Import JSON control, staff-only preview/confirm endpoint `/sources/import/`, fixed example/schema/guide downloads, strict JSON/URL validation and atomic insert-only creation. New sources remain paused and unapproved; existing sources are never edited. No model migrations or dependency changes.

Operator format guide: `docs/BULK_SOURCE_IMPORT.md`.
Editable template: `examples/bulk-sources.json`.
Structural schema: `examples/bulk-sources.schema.json`.

## Verified execution

Candidate was exercised using the installed Python 3.11.15 interpreter with sanitized environment, isolated disposable data directories, provider/search settings disabled and inherited loopback-only network guard. Production dependencies were reused read-only; no packages were installed into production.

- Full Django suite: **276 tests, OK**.
- New bulk-import test module: service and browser coverage, including defaults, authorization, CSRF, strict JSON, URL safety, escaping, signed-token expiry/tamper/account binding, duplicates, competing insert, confirmation-time duplicate recheck and database rollback.
- `manage.py check`: no issues.
- `manage.py makemigrations --check --dry-run`: no changes.
- Dependency health: no broken requirements.
- `scripts/smoke_bulk_import.py`: actual loopback HTTP multipart upload, preview with no writes, signed confirmation, repeated confirmation, rejected invalid batch, CSRF rejection and downloads; new sources inactive/unapproved and no jobs or paid attempts.
- `scripts/smoke_local.py`: actual web/worker process, synthetic collection, review/CSV persistence, restart, discovery and automation checks passed.
- `scripts/smoke_jev.py`: actual synthetic web/worker, reservation/concurrent admission and metadata-import checks passed; no paid or external requests.
- All three published JSON examples validated against Draft 2020-12 schema and actual importer.
- Independent read-only review: passed, no security concerns or logic errors. Non-blocking regression/documentation suggestions were incorporated. Production implementation did not change after review; additions were tests and documentation.
- Static added-line scan: no hardcoded credentials, shell injection, eval/exec or pickle findings. `git diff --check` clean.

An attempted standalone `scripts/smoke_discovery.py` command failed because that script does not exist. Discovery is covered by the successful `smoke_local.py`; it is not represented as a separate passing command. Initial missing-static-directory test warning was resolved by collecting candidate static assets before the final suite.

Evidence directory: `/home/crono/.hermes/profiles/lead-scrape/cache/bulk-import/` (`final-suite.log`, `final-http-smoke.log`, `checks.log`, `migration-drift.log`, `smoke-local.log`, `smoke-jev.log`, `docs-app-validation.log`, `independent-review.json`). The local `run.py` there encapsulates sanitized execution; its paths are machine-specific.

## Deployment boundary

Deployment remains a separate step. Before an authorized rollout, recheck production drift, preserve a verified encrypted backup of database/private configuration/service state, stop the existing owner safely, install only this verified revision, and verify the existing private listener and single worker with all collection gates preserved. Do not import example domains, approve sources, enable paid routing or change campaign quotas as part of deployment. No schema migration is introduced by this change.

The importer intentionally stages Sources rather than adding discovery campaign candidates, so no campaign ID or inventory-cap change is required. Manual source approval and a separate collection start remain required. An accepted hostname is not proof of reachability or permission; authorized transport performs DNS/public-address and robots checks later.
