# ClearPay Lead Engine

This is a local-first Python/Django application that builds and refreshes a database of publicly listed business sales representatives. Read README.md and docs/ARCHITECTURE.md before substantial changes.

The GitHub repository is `crono117/crono_crawler`. Base new work on `main`. The branch `archive/legacy-scraper-2026-09-16` preserves the repository state before this application was added; treat it as a historical snapshot and do not merge development changes into it.

## Run it

- Python 3.12 is the reference environment; Python 3.11+ is supported by the bootstrap.
- Install and migrate: `python3 setup.py --no-user`
- Tests: `.venv/bin/python manage.py test`
- Live web/worker smoke test: `.venv/bin/python scripts/smoke_local.py` (isolated temporary database, synthetic contacts, no external requests).
- Django checks: `.venv/bin/python manage.py check`
- Migration drift: `.venv/bin/python manage.py makemigrations --check --dry-run`
- Web and worker: `python3 run-local.py`
- Synthetic data: `.venv/bin/python manage.py init_demo`; the running worker processes it.
- Create a local operator interactively: `.venv/bin/python manage.py createsuperuser`.

## Architecture and invariants

- Keep the core in Python. Django renders the control panel; SQLite is the local default and PostgreSQL is the server option.
- No OpenAI API or other hosted model is required. Optional model extraction uses a separately configured Ollama service.
- There is exactly one active worker per database. The database queue, per-origin throttle, and lease are restart checkpoints.
- Never fetch unapproved sources. Respect robots rules and configured origin/path limits. Pause on blocked access. Do not add CAPTCHA solvers, proxy rotation, stealth, or login/session scraping.
- Network destinations are public-only. Keep DNS pinning and redirect checks in the HTTP path and request interception in the browser path.
- Preserve the distinction between source business type, services in person evidence, and services elsewhere on the page.
- Contact details and names require stored source evidence. Unknown attributes stay unknown. AI output is untrusted and must pass evidence validation.
- Keep review notes and suppression decisions across recrawls. CSV exports exclude suppressed and rejected records and escape formula cells.
- Do not commit .env, credentials, real contact exports, runtime databases, browser profiles, or collected HTML. Use synthetic fixtures for tests.
- Worker and webapp must remain separable processes. Do not start workers inside Django request handlers or AppConfig.ready().

## Code map

`leads/services/network.py`: public HTTP/browser transport and URL scope.
`leads/services/extraction.py`: CSS and Ollama extraction and evidence validation.
`leads/services/storage.py`: conservative identity and observations.
`leads/services/worker.py`: scheduling, leases, retries, robots and crawl limits.
`leads/models.py`: persistent data model. Commit migrations for schema changes.
`leads/forms.py`, `views.py`, `templates/`, `static/`: authenticated operator console.

## Change verification

Run the tests and Django checks after code changes. Add a regression test when fixing a meaningful collection, privacy, authentication, or persistence defect. Keep network tests deterministic; no third-party websites or hosted LLMs are required. Run actual browser/Ollama integration checks only when those optional services are available, and distinguish mock tests from real integration results.

The included Cursor environment starts an empty local database with no administrator and no approved real sources. Configure access in Cursor, then use its guided environment build. The environment configuration has not yet been exercised in a Cursor cloud VM.
