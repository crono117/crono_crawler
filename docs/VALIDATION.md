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

## Discovery branch verification

The `feat/discovery-v1` extension passed **72 Django tests** (the original 40 plus 32 discovery tests), Django system checks, and the migration-drift check. Two existing assertions now expect an external link's exact path rather than only its homepage.

Discovery coverage includes the offline graph and sitemap-only contact page, source approval/revocation, source/campaign pause propagation, recurring schedules and queue coalescing, bounded URL/domain/job budgets, persistent daily quotas, ranking and tracking-parameter removal, dismissal/review, evidence preservation on extraction errors, XML/gzip limits and entity rejection, optional search contracts and shared quota/backoff, staff access, CSRF, server-side form limits and all new templates.

Four integration tests use real local HTTP sockets with only fixture DNS/socket routing substituted. They validate robots-first behavior and throttling, nested sitemaps (including a map without an XML filename), preservation of exact external URLs without fetching them, explicit approval before fetching a second domain, lead extraction, HTTP 403 pauses, disallowed robots, and blocked private redirects. Invalid/private sitemap entries are skipped without weakening network guards.

The extended `scripts/smoke_local.py` passed with actual separate web and worker processes. It creates a temporary database, runs the existing collection/review/export checks, exercises the discovery dashboard, follows the fixed offline graph into three additional fictional contacts, dismisses a discovered URL, pauses the campaign, and restarts both processes with discovery work queued. The repeated run adds no duplicates, preserves URL dismissal, and retains existing lead suppression. Processes and the temporary database are cleaned up.

No real lead website, live Brave API request, or visual browser screenshot was used to validate this feature. Discovery's HTML-first path does not establish that JavaScript-only sources work. The existing optional browser/Ollama/Docker/Cursor validation gaps below remain separate.

## Host Merchant follow-up verification

The follow-up passed **92 Django tests**, system checks and the migration-drift check. No database schema change is required. Twenty added tests cover scoped evidence selectors, unchanged strict contact/role validation, distinct rejection diagnostics, the read-only offline preview command, path/domain/plural exclusions, navigation noise, rescoring already saved URLs, skipping queued excluded URLs, manual approval for high-scoring new domains, retrospective zero-contact alerts and clearing current alerts on a refresh that sees existing contacts.

The actual web/worker smoke test also passed with the added zero-contact fixture. Its worker completed a one-page run with no matching card selector; the lead overview, Discovery dashboard, campaign detail and run detail all displayed the recipe-review warning, and the page diagnostics identified the unmatched selector. Original collection, evidence, suppression, export, restart and discovery checks still passed. This used an isolated temporary database with fictional contacts only.

The user's desktop report establishes a successful 10-page Host Merchant crawl with zero validated contacts; it is operator-reported, not a crawl performed in this workspace. Official public-page text was inspected for suitability. Direct fetching could not resolve the domain here, and the separate cloud browser encountered the site's security verification, so no Host Merchant HTML selectors or production extraction recipe were validated. See [the Hermes handoff](HOST_MERCHANT_PILOT.md) for the local inspection and 5–10-page rerun. The update must still be downloaded and exercised against the user's actual campaign before claiming improved real-contact yield.

## Still to validate on the user's environment

- **Chromium rendering:** Playwright was installed in a separate build environment, but full Chromium and a subsequent headless-shell download timed out. The browser collector has not been exercised with a real browser. The separate browser-control service rejected navigation to the local application with `ERR_BLOCKED_BY_CLIENT`, so no visual screenshot QA was completed; templates were exercised through Django and real HTTP.
- **Ollama extraction:** schema/evidence validation was tested with mocked model output. No local model was available for an actual inference test.
- **Docker/PostgreSQL deployment:** configuration is included, but Docker was unavailable, so image builds, Compose startup, PostgreSQL migration and data transfer remain unexecuted.
- **Cursor cloud environment:** configuration is included, but a Cursor cloud VM build has not been exercised.
- **Real source coverage:** no real contacts were harvested. A direct transport probe to `https://example.com/` failed at DNS resolution in this workspace. Each approved site still needs its recipe, access behavior and extraction quality verified on a host with public-network access before an unattended schedule is relied upon.
- **Windows/macOS:** Python launch scripts are designed for them but were exercised only on Linux.

These are specific pilot validation gaps, not evidence that optional integrations have passed. Start with the verified HTML/offline workflow, then enable and test optional capabilities individually.
