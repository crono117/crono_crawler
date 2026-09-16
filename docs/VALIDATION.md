# Validation record

Initial build: September 16, 2026. Reference environment: Linux, Python 3.12.14, Django 5.2.17, SQLite. A fresh project-local virtual environment was created by the actual bootstrap script.

## Passed

- `python3 setup.py --no-user --demo`: dependency installation, random private configuration, migrations, and fictional source queueing.
- `python manage.py test`: **37 tests passed**. Tests cover extraction evidence, separate person/page tags, conservative deduplication, shared contacts, review persistence, changed-page freshness, cache invalidation, source approval, queue coalescing, pause/resume, worker leases and restart recovery, scheduling, robots controls, Retry-After, blocked-source handling, bounded discovery, private-address/redirect rejection, contact substring rejection, authentication, CSRF, CSV suppression/formula escaping, form limits and page rendering.
- `python manage.py check`: no issues.
- `python manage.py makemigrations --check --dry-run`: no model/migration drift.
- `python -m pip check`: no broken dependency requirements in the clean local environment.
- A live HTTP smoke test started the actual `run-local.py` launcher, signed in with a temporary random-password staff account, loaded the lead/source/run pages and CSS, verified three fictional contacts collected by the worker, then stopped the launcher and verified that it released the worker lease. The temporary account was removed.

The core test suite uses synthetic HTML and mocked third-party network/model responses. It does not make requests to real lead sources.

## Still to validate on the user's environment

- **Chromium rendering:** Playwright was installed in a separate build environment, but the Chromium download repeatedly timed out. The browser collector has not been exercised with a real browser in this build. No visual screenshot QA was completed; the HTML templates were exercised through Django and real HTTP.
- **Ollama extraction:** schema/evidence validation was tested with mocked model output. No local model was available for an actual inference test.
- **Docker/PostgreSQL deployment:** configuration is included, but Docker was unavailable, so image builds, Compose startup, PostgreSQL migration and data transfer remain unexecuted.
- **Cursor cloud environment and GitHub Actions:** configuration files are included. They have not run in those hosted environments yet.
- **Real source coverage:** no real contacts were harvested. Each approved site needs its recipe, access behavior and extraction quality verified before an unattended schedule is relied upon.
- **Windows/macOS:** Python launch scripts are designed for them but were exercised only on Linux.

These are specific pilot validation gaps, not evidence that optional integrations have passed. Start with the verified HTML/offline workflow, then enable and test optional capabilities individually.
