# Validation record

Initial build: September 16, 2026. Reference environment: Linux, Python 3.12.14, Django 5.2.17, SQLite. A fresh project-local virtual environment was created by the actual bootstrap script.

## Passed

- `python3 setup.py --no-user --demo`: dependency installation, random private configuration, migrations, and fictional source queueing.
- `python manage.py test`: **40 tests passed** after the follow-up workspace verification. Tests cover extraction evidence, separate person/page tags, conservative deduplication, shared contacts, review persistence, changed-page freshness, cache invalidation, source approval, queue coalescing, pause/resume, worker leases and restart recovery, scheduling, robots controls, Retry-After, blocked-source handling, bounded discovery, private-address/redirect rejection, contact substring rejection, authentication, CSRF, CSV suppression/formula escaping, form limits and page rendering.
- `python manage.py check`: no issues.
- `python manage.py makemigrations --check --dry-run`: no model/migration drift.
- `python -m pip check`: no broken dependency requirements in the clean local environment.
- A live HTTP smoke test started the actual `run-local.py` launcher, signed in with a temporary random-password staff account, loaded the lead/source/run pages and CSS, verified three fictional contacts collected by the worker, then stopped the launcher and verified that it released the worker lease. The temporary account was removed.
- Three added HTTP integration tests used a real local HTTP server and sockets to exercise robots fetching, HTTP response parsing, two-page collection, four evidence observations for three deduplicated people, unchanged-page recrawling, suppression persistence, 403 pausing and rejection of a redirect to a private destination. Only the fixture hostname's DNS answer and connection destination were substituted inside the test; production access controls were not changed.
- `python scripts/smoke_local.py`: separate Django web and collector processes passed real HTTP sign-in, CSS delivery, service filtering, evidence display, Do not contact review, CSV exclusion, pause/run controls, deduplication, and full process restart with a persisted queued job. The login session and review decisions survived the restart. All data and credentials were generated in an isolated temporary database and removed afterward. This repeatable check is now included in GitHub Actions.
- The initial published application's [GitHub Actions run](https://github.com/crono117/crono_crawler/actions/runs/35130722703) also passed.

The test suite uses synthetic HTML, mocked third-party model responses, and controlled local HTTP fixtures. It does not make requests to real lead sources. These checks validate application behavior, not real-site extraction coverage or public-network connectivity.

## Still to validate on the user's environment

- **Chromium rendering:** Playwright was installed in a separate build environment, but full Chromium and a subsequent headless-shell download timed out. The browser collector has not been exercised with a real browser. The separate browser-control service rejected navigation to the local application with `ERR_BLOCKED_BY_CLIENT`, so no visual screenshot QA was completed; templates were exercised through Django and real HTTP.
- **Ollama extraction:** schema/evidence validation was tested with mocked model output. No local model was available for an actual inference test.
- **Docker/PostgreSQL deployment:** configuration is included, but Docker was unavailable, so image builds, Compose startup, PostgreSQL migration and data transfer remain unexecuted.
- **Cursor cloud environment:** configuration is included, but a Cursor cloud VM build has not been exercised.
- **Real source coverage:** no real contacts were harvested. A direct transport probe to `https://example.com/` failed at DNS resolution in this workspace. Each approved site still needs its recipe, access behavior and extraction quality verified on a host with public-network access before an unattended schedule is relied upon.
- **Windows/macOS:** Python launch scripts are designed for them but were exercised only on Linux.

These are specific pilot validation gaps, not evidence that optional integrations have passed. Start with the verified HTML/offline workflow, then enable and test optional capabilities individually.
