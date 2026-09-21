# Parallel extraction trial and local-agent handoff

Branch: **`feat/jev-extraction-packs`** in `crono117/crono_crawler`.
Base: published Jev test commit `125b7c455d370af07625250b26dfab687956411b` on `feat/jev-integration-v1`.
The original test branch is unchanged. Use a separate clone and database for this comparison.

This adds deterministic platform hints, reusable CSS recipe suggestions, and Extruct parsing to the existing single-worker system. No Jev key is needed for the tests, demo or benchmark. Jev still classifies companies and people; this addition does **not** add paid Choice calls for platform detection.

## Give this task to a local agent

> Clone `crono117/crono_crawler`, branch `feat/jev-extraction-packs`, into a new directory. Read AGENTS.md and docs/EXTRACTION_TESTING.md. Use an isolated database. Run the full tests, Django checks, migration drift, all three smoke scripts, and the offline comparison with its separate AutoScraper environment. Then enable the extraction flag with Jev in mock mode, run the five-layout demo, and inspect Site automation, lead evidence and Jev evidence. Report the commit, test results, accepted person/contact pairs, rejected associations, and any failures. Keep API calls disabled. When a Jev key is available, follow docs/JEV_TESTING.md for the bounded live calibration. Do not merge or replace the existing installation.

## Install and verify

Reference environment: Linux and Python 3.12. Use a fresh shell whose `DATA_DIR` and `DATABASE_URL` do not point to another installation. The bootstrap creates a private local `.env` and database.

```bash
git clone --branch feat/jev-extraction-packs --single-branch https://github.com/crono117/crono_crawler.git crono-crawler-extraction-trial
cd crono-crawler-extraction-trial
python3 setup.py --no-user
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
.venv/bin/python scripts/smoke_local.py
.venv/bin/python scripts/smoke_jev.py
.venv/bin/python scripts/smoke_extraction.py
.venv/bin/python manage.py benchmark_extraction --assert-fixtures
```

All smoke scripts use temporary databases and fictional fixtures. The new smoke starts a real worker, takes all five layouts through probe/validation/canary, checks Jev mock judgments and evidence spans, restarts and re-probes, and verifies that suppression and deduplication survive. No database migration is added by this extension.

AutoScraper is optional and needs a **separate environment**. Version 1.1.14's empty-attribute matching does not replay the training fixtures with the app's BeautifulSoup 4.15.0; its comparison environment pins BeautifulSoup 4.12.3. Do not install `requirements-benchmark.txt` into the app environment.

```bash
python3 -m venv .venv-autoscraper
.venv-autoscraper/bin/python -m pip install -r requirements-benchmark.txt
.venv/bin/python manage.py benchmark_extraction \
  --autoscraper-python .venv-autoscraper/bin/python --assert-fixtures
```

The command prints JSON and saves no contacts. Both comparison processes block socket connections and requests HTTP calls. They train/select rules on `*-train.html` only. Different names and addresses appear in the held-out pages. `*-stable.html` preserves layout; `*-heldout.html` also adds wrappers and reorders records. No fixture contains real people or companies.

## Run the interactive demo

Set these values in the trial `.env`, then start/restart both web and worker with that configuration:

```dotenv
EXTRACTION_PACKS_ENABLED=1
JEV_MODE=mock
JEV_CAPTURE_ENABLED=1
JEV_ROUTING_ENABLED=0
```

```bash
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py init_extraction_demo
python3 run-local.py
```

Open `http://127.0.0.1:8017`. **Site automation** should show five active setups after canaries pass. The two fictional people, Robin Demo and Morgan Sample, have ten source observations across five layouts. Re-running the demo re-probes without duplicating observations or losing review notes/suppression. **Jev evidence** includes parsed structured spans and mock judgments. Paid attempts remain zero. Mock results demonstrate plumbing, not model accuracy.

## What is integrated

| Component | Integration |
|---|---|
| WordPress | First-party suggestion for `.wp-block-group` / `.wp-block-heading` layouts |
| Webflow | First-party suggestion for `.w-dyn-item` CMS collection cards |
| Squarespace | First-party suggestion for `.list-item` user-list cards and their content classes |
| Extruct 0.18.0 | Bounded JSON-LD and Microdata Person/Organization parsing from already-fetched HTML |
| Jev candidate capture | Structured people, explicit employers and direct contact properties enter the existing evidence pipeline before the accepted-sales-contact filter |
| AutoScraper 1.1.14 | Offline, isolated train/held-out comparison; no worker integration or contact promotion |

These are initial layout patterns, not universal templates for every theme, plugin or site built on a platform. Multiple hints may coexist; a hint alone establishes neither architecture nor ownership. Generic recipe proposals remain available when platform-specific suggestions do not apply. No market-share or internet-coverage percentage is assumed.

The small first-party hint list is based on DOM attributes, asset paths and known containers. No Wappalyzer/WebAppAnalyzer fingerprint dataset is copied. No second crawler framework or fetch path is added.

Automatic setup orders the new suggestions before generic candidates, tests them on the saved local probes, and uses the existing readiness threshold, versioning, bounded canary and drift monitoring. The canary must pass before accepted leads are stored. Source approval, robots, origin/path scope, DNS checks, throttling, quotas and the single collector lease remain in the existing fetch path.

Structured data can also be selected explicitly on an operator-configured source with:

```json
{"engine": "structured-v1"}
```

Use `examples/extraction/structured-recipe.json` with the existing saved-HTML preview:

```bash
.venv/bin/python manage.py inspect_recipe --source SOURCE_ID \
  --html /absolute/path/to/authorized-saved-page.html \
  --recipe examples/extraction/structured-recipe.json --show-records
```

This is read-only. Automatic setup still requires its validation/canary flow. Existing operator-configured CSS recipes are not silently replaced by enabling the flag.

## Structured-data boundaries

Only direct `Person` properties (`name`, `jobTitle`, `email`, `telephone`) and an explicit inline `worksFor` Organization name are used. Top-level JSON-LD and `@graph` with a simple schema.org context are supported. Root Microdata Person/Organization scopes are supported, including `meta content` values. Multiple conflicting field values are not arbitrarily chosen. Nested Article authors, remote `@id` joins, custom contexts, Microdata `itemref` joins and inferred employment are excluded. These conservative bounds can miss valid real-world data.

No remote context, API, sitemap or additional URL is fetched by Extruct. The parser selects only JSON-LD and Microdata, caps input at 6 MiB of characters, limits structured blocks to 64 KiB of characters and 256 KiB total, and examines at most 100 structured nodes. These character limits sit inside the existing fetch byte limit. Malformed/over-limit structured probes cannot meet the default automatic release threshold. Candidate evidence is limited to 1,200 characters per entity and the existing 50,000-character document budget.

Evidence is explicitly labelled **parsed direct properties**, with canonical JSON, syntax/locator, original response hash, parser/extraction version and exact spans into the saved derived document. It is not presented as byte-for-byte source HTML or visible prose. Descriptions, employer inboxes and unrelated nested people are excluded from that projection. The HTML probe retains its existing private 24-hour lifetime; candidate text/contact payloads retain the existing 30-day expiry.

People without channels can remain Jev candidates when explicit employer evidence exists. They do not become accepted leads. Structured accepted contacts require the current source's role rule and a usable personal channel; generic, repeated and footer channels are removed. Presence of an address does not prove exclusive ownership or deliverability. Jev judgments do not override source evidence or human suppression.

Changing the feature flag changes extraction/evaluation cache signatures. Disabling it blocks collection with a structured recipe and pauses a pending structured release. Previously validated CSS recipes are still ordinary CSS recipes. Use the same flag in web and worker processes.

## Verification and interpretation

Local results on September 21, 2026: **221 tests passed** (194 inherited, 27 added); Django checks, migration drift, all three process smoke scripts, and the held-out fixture assertion passed. The fixture result is **10/10 expected person/email pairs, zero extra pairs**, versus zero accepted pairs from the pre-addition proposal set on these deliberately selected missing-pattern fixtures. This is a regression demonstration, not a production accuracy or coverage estimate.

AutoScraper reports individual field recovery separately and explicitly does not validate person/contact association. Its training replay and stable-layout control make dependency failures distinguishable from layout drift. Inspect those controls before interpreting held-out results. The benchmark is intentionally too small to justify production deployment of learned rules.

| Synthetic case | New recipes: changed-layout pairs | AutoScraper: stable-layout fields | AutoScraper: changed-layout fields |
|---|---:|---:|---:|
| WordPress | 2/2 | 4/4 | 0/4 |
| Webflow | 2/2 | 4/4 | 0/4 |
| Squarespace | 2/2 | 4/4 | 0/4 |
| JSON-LD | 2/2 | 0/4 | 0/4 |
| Microdata | 2/2 | 4/4 | 0/4 |

AutoScraper's JSON-LD case also fails training replay; this raw-HTML field-learning harness does not support script extraction. The assertion checks its training and stable-layout controls for the other four cases. Its field counts are not interchangeable with validated person/contact pairs.

Live Jev, actual API billing/model behavior, live website quality, browser-rendered pages and production throughput have not been validated by this addition. After obtaining the key, follow the existing [bounded live calibration](JEV_TESTING.md#live-calibration-after-the-key-arrives), keeping this branch and `EXTRACTION_PACKS_ENABLED=1` when importing structured evidence.

For comparison or rollback, stop the trial processes and return to the original checkout with its original database. Do not run the old branch against the trial database while the new worker is active.

Dependencies: [Extruct source and BSD-3-Clause license](https://github.com/scrapinghub/extruct), [AutoScraper source and MIT license](https://github.com/alirezamika/autoscraper). They are installed as pinned packages; their source is not vendored. See `automation/packs.py` for the small first-party recipe registry.
