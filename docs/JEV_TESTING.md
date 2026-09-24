# Jev implementation and local-agent handoff

**Current local rollout:** use [LOCAL_JEV_ROLLOUT.md](LOCAL_JEV_ROLLOUT.md) for verification, recovery semantics, the $2-per-UTC-day policy and deployment/rollback steps. This guide is not independent authorization to spend or deploy.

Branch: `feat/jev-integration-v1` in `crono117/crono_crawler`.

This is an opt-in Python/Django implementation on the discovery/automation baseline. It runs without a Jev key using a fixed mock provider. All paid classification goes through one persisted admission path and the existing collector lease. No new daemon, queue service or Python dependency is required.

**Live Jev has not been called in this build.** Mock classifications demonstrate plumbing, not model accuracy. Model/account access, pricing, actual response shape and usage reporting must be checked with your key before using real classifications. The original [design plan](JEV_DESIGN.md) is a historical proposal; this guide describes the implemented behavior and its limits.

## Give this task to a local agent

> Clone `crono117/crono_crawler`, branch `feat/jev-integration-v1`, into a new directory. Follow `docs/JEV_TESTING.md`. Use a new test database and keep the existing installation untouched. Run Django checks, migration drift, the full tests, `scripts/smoke_local.py`, and `scripts/smoke_jev.py`. Then run `manage.py jev demo`, inspect the Jev evidence screen, and report failures with sanitized logs. Keep real API calls disabled until the key is supplied and the live-calibration section is explicitly enabled. Never print or commit the key. Do not merge into main or replace the production database.

## Download and verify, no key needed

Use Python 3.12 on Linux/macOS. Python 3.11+ is supported by the bootstrap; this branch's process checks were run on Linux/Python 3.12. On Windows, substitute `.venv\Scripts\python.exe`; Windows process behavior has not been exercised here.

```bash
git clone --branch feat/jev-integration-v1 --single-branch https://github.com/crono117/crono_crawler.git crono-crawler-jev-trial
cd crono-crawler-jev-trial
python3 setup.py --no-user
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
.venv/bin/python scripts/smoke_local.py
.venv/bin/python scripts/smoke_jev.py
.venv/bin/python manage.py jev doctor
.venv/bin/python manage.py jev demo
```

Before bootstrap, ensure inherited `DATA_DIR` and `DATABASE_URL` do not point to your existing installation. A fresh clone defaults to its own `data/leads.sqlite3`. `.env` is generated with a private secret and is ignored by git. Environment variables override `.env` values.

The smoke scripts use separate temporary databases. They force Jev/search/Ollama settings to safe fixture values and make no third-party requests. `smoke_jev.py` starts real web and worker processes, signs in over localhost, checks evidence and POST/CSRF controls, restarts the processes, then races two independent admission processes on one SQLite file. Only one can reserve; neither sends an API request. Recovery retains the uncertain reservation.

Expected `jev demo` outcome on a fresh database:

| Check | Expected |
|---|---|
| People | Jordan Example, Software Engineer, no personal contact; Riley Example, Account Executive |
| Evaluations | 4 succeeded mock evaluations, 15 judgments |
| Company follow-up | One completed Aster Systems job, 2 fixture fetches, 0 paid attempts |
| Shared contacts | Support email and switchboard remain shared, deliverability untested |
| External link | `unapproved.example.org/sales/` stays pending manual review |
| Paid attempts / spend | 0 / $0 |
| Running demo again | Same evaluations and company job; cached evidence uses recorded |

## Inspect the console

```bash
.venv/bin/python manage.py createsuperuser
python3 run-local.py
```

Open **http://127.0.0.1:8017/classification/**. The launcher uses port 8017. Sign in with a staff account. Inspect source URLs, retrieval times, exact evidence spans, labels, complete probability distributions, provider/model versions, attempts and cached uses. Review a judgment as confirmed, rejected or needing evidence; the reason is audited. Review does not promote a contact or overwrite lead suppression/notes.

`jev demo` is a synchronous offline command: stop a running worker before invoking it. It selects only its own fixed fixtures even in a populated database. Alternatively, use `jev demo --seed-only`, set `JEV_MODE=mock`, `JEV_CAPTURE_ENABLED=1`, `JEV_ROUTING_ENABLED=1`, and start the normal worker to observe the same queue flow. Mock outcomes can trigger routing only for sources with the `demo` collector.

## Review-only layered candidate blocks

`JEV_LAYERED_BLOCKS_ENABLED=1` adds a default-off fallback for authorized pages whose person blocks are not already captured. It requires `JEV_CAPTURE_ENABLED=1`. Eligibility is decided from usable candidates, not raw selector matches: a CSS row counts only when it names one plausible person with bounded card evidence, and a structured `Person` counts only when it states an employer. A block that is, contains or sits inside a captured card, or whose own heading names exactly a captured person, is skipped so one person is not queued twice; a name merely mentioned in a block does not count. Junk selector matches and employer-less schema authors therefore no longer suppress the fallback (see [Lead yield work package 1](LEAD_YIELD.md)).

Code—not Jev—builds at most six bounded candidate blocks from already-fetched HTML. Header, navigation, footer, forms, dialogs, templates, hidden content, nested duplicates and blocks without a plausible name, role and published contact signal are excluded. Each block is capped at 500 characters and all blocks together at 2,400 characters. Exact block text is stored in immutable evidence spans.

Jev receives only supplied block IDs and typed questions about page purpose and block type. It cannot return selectors, names, emails, phone numbers, URLs, evidence quotes, permissions or budgets. Successful judgments remain review-only: they do not create people, contacts or leads; change recipes; approve sources; register URLs; or start routing. Company follow-up jobs are excluded from this initial fallback so their evaluation packet budget is unchanged.

The feature is intentionally an instrumentation slice. It measures whether model-assisted segmentation can identify useful person/profile blocks on layouts missed by deterministic selectors. A later phase must still parse selected blocks mechanically and pass the existing evidence/contact validation before lead yield can improve.

## Live calibration after the key arrives

Start with saved evidence and routing off. Stop the normal worker so a one-request CLI trial cannot compete with it.

1. Check your TypeSafe account has access to **`jev-1.13.0`**, and verify the current API contract and input price. This implementation pins that model and uses **$0.042 per million input tokens**, zero output-token charge. See the [official API reference](https://docs.typesafe.ai/api). If model, price or billing semantics differ, update the adapter/accounting/tests before confirming readiness.
2. Put the key in the private `.env` as `TYPESAFE_API_KEY=...`. Do not put it in source files, command history, prompts or logs. Set `JEV_MODE=live`, `JEV_PRICE_CONFIRMED=1`, `JEV_ROUTING_ENABLED=0`, and keep `JEV_CAPTURE_ENABLED=0` for this first saved-evidence test.
3. Choose token admission mode. The strict default requires `JEV_TOKEN_COUNTER=your_module.count_request`, a provider-verified callable accepting the **entire request dict** and returning its input-token count, including questions, criteria and provider framing. No verified TypeSafe tokenizer is bundled. For a deliberately bounded initial calibration, explicitly set `JEV_ALLOW_ESTIMATED_TOKENS=1` instead. The fallback uses UTF-8 envelope bytes × 1.25 + 512. It is an estimate, **not a guarantee of the 5,000-token ceiling**. Above-limit reported usage pauses subsequent requests.
4. Run `jev doctor`. It reads configuration and checks ledger consistency; it does not probe the account. Correct reported configuration problems. Confirm the console is unpaused, has no unexplained active attempt, and reports the intended UTC-day dollar allowance.
5. Create/review a real Source in the existing console with its exact approved origin and paths. Import HTML already retrieved with that authorization, supplying its actual timezone-aware retrieval time:

```bash
.venv/bin/python manage.py jev import-html \
  --source SOURCE_ID \
  --url https://approved.example/team/ \
  --file /absolute/path/to/authorized-saved-page.html \
  --retrieved-at 2026-09-21T12:00:00+00:00 \
  --provider live
```

Replace all placeholders and the example timestamp. Import makes no network requests. It returns evaluation UUIDs. An empty list means the source/scope or deterministic candidate evidence did not qualify. A person may be retained without a contact; a source/company name and bounded person card must still have page evidence.

6. Select one returned UUID and deliberately make **at most one attempt**:

```bash
.venv/bin/python manage.py jev run --live --evaluation EVALUATION_UUID --limit 1
.venv/bin/python manage.py jev report
```

Inspect the result in the console: actual model, question IDs, labels, finite normalized probabilities, confidence, input/output usage, attempt status and cost. Invalid business answers never become judgments, but valid reported usage is still charged to the ledger. Missing usage keeps the full reservation. Choice probabilities must form a distribution. Exact/near-exact sums are accepted; bounded provider rounding drift of at most 0.01 is deterministically normalized, stored as normalized probabilities and recorded in a sanitized `normalize_provider_probabilities` control event. Larger drift remains `failed_contract` with only answer ordinal, choice count, sum and delta in the diagnostic. A `failed_contract` result needs contract investigation, not blind retries. Compare with human labels before increasing the limit or enabling routing.

`--limit` bounds processing attempts per command invocation. It does not bypass persisted cooldowns, the UTC-day dollar allowance, the three-attempt per-evaluation ceiling or evidence checks. Future runs skip work until its deadline is due. Mock and live cache identities are distinct.

## Enable a small company pilot after reviewing shadow results

Use the existing source/campaign approvals. Review company-domain relationships in **Jev evidence**; the domain must occur in saved source evidence. Verifying a relationship does not approve collection. New company domains appear as manual-review metadata in Discovery, including when automatic site policy is enabled. Approve the exact source/path deliberately using existing source/discovery controls. Automatic sources still require recipe validation and canary success.

Create an active campaign with only the intended sources, then create a bounded pilot:

```bash
.venv/bin/python manage.py jev pilot --campaign CAMPAIGN_ID --name "Jev first company trial" --hours 24 --activate
```

Set `JEV_CAPTURE_ENABLED=1` and `JEV_ROUTING_ENABLED=1`, retain the reviewed live gates, and restart the normal web/worker process. The original campaign scheduler also remains active with its own budgets; company-job limits apply to work owned by that company job, not to unrelated existing campaign runs. Use an isolated test campaign for measurement.

Only a fresh **human-confirmed** company-level technology/provider result meeting the fixed probability, confidence and margin thresholds can trigger follow-up. `needs_review`, `needs_evidence` and rejected live judgments cannot route. The packaged offline demo remains the only mock/demo exception and cannot target real sources. An engineer's title is not treated as a sales role. Stored exact links and the approved seed are used; no guessed paths, generated domains, search calls or off-origin redirects are introduced by the company router. A job deduplicates across repeated triggers and does not recursively launch further company jobs from its own evaluations.

## Controls, accounting and recovery

| Control | Implemented bound / behavior |
|---|---|
| Defaults | Mode off, capture off, routing off; a key alone does nothing |
| Money | $2 per UTC day by default; settled cost plus unresolved reservations; integer nano-USD |
| Admission reserve | 66,000 input tokens × 42 nano-USD = $0.002772 per attempted call; conservative full-context reservation |
| Attempt counts | No daily or cumulative paid-attempt admission ceiling; per-evaluation retry bounds remain |
| Daily money | `JEV_DAILY_ALLOWANCE_USD` caps each UTC day's settled charges plus unresolved reservations. Admission requires room for the full conservative reservation; live mode fails closed without a positive cap. |
| Cumulative ledger | Lifetime spend and attempts remain durable and auditable but do not stop admission |
| Concurrency / pacing | One locally active paid request; next admission at least one second after completion |
| Retries | Up to 3 attempts per evaluation; automatic retry only with known usage for 429/529/500/502/503/504. Unknown usage/transport failure requires explicit recovery; persisted jitter and Retry-After remain. |
| Transport | Fixed HTTPS endpoint; zero transport retries; redirects disabled; 30-second total API timeout; 256 KiB response bound |
| Evidence | Hashes, exact character spans, retrieval time, parser/extraction version, current approved scope; payload/cache separates mock/live |
| Company work | 10 pages, depth at most 2, 30 fetch attempts including robots/redirects/retries, 5 distinct evaluation packets, 10 paid attempts, 24-hour default job deadline |
| Pilot work | 10 companies, 300 fetches, 100 model attempts, 25 external/domain proposals; 5 external proposals per company |
| Processing allowance | Persisted 30-second work units; maximum 900 units-seconds per company. This is admission accounting, **not a measured 15-minute wall-clock guarantee for website transport/DNS** |
| Retention | Candidate text, extracted contact values and model request payloads expire after 30 days. Audit hashes, names/affiliations, judgments and money history remain; this is not a full personal-data deletion facility |
| Routing freshness | Both the latest evidence check and successful evaluation must be within 7 days; cache keys use seven-day windows. Original retrieval times remain immutable |
| SQLite | WAL, synchronous FULL, IMMEDIATE write transactions for every app process |

Reservations are debited before the only Jev HTTP call. Admission and dispatch revalidate current approval/scope/extraction identity plus exact request-question bindings, persisted span ownership/hashes, transmitted span text and contact-candidate fields. Keep the capture-time collector, extractor, recipe and scope identity in place until selected evaluations finish; restoring it first correctly invalidates the queued evidence. Response usage reconciles money even if answers fail validation. Timeouts and missing usage keep reserved money. Authentication failures and reported token/reservation overruns pause admission. Unknown crashes retain the dispatch owner even after the worker lease expires; restart cannot silently refund or resend.

```bash
.venv/bin/python manage.py jev pause --reason "Review initial calibration"
.venv/bin/python manage.py jev resume --reason "Account and evidence checked"
.venv/bin/python manage.py jev pilot-state PILOT_ID pause --reason "Review outcomes"
.venv/bin/python manage.py jev pilot-state PILOT_ID resume --reason "Continue existing limits"
```

For a crashed attempt: stop the old worker/transport, inspect the specific attempt and account billing, then release the owner explicitly. **Do not assume a timeout means free.**

```bash
.venv/bin/python manage.py jev recover ATTEMPT_UUID --worker-stopped --reason "Old process stopped; billing still unknown"
```

This preserves its full reservation. When billing is independently verified, repeat with `--billed-nusd VERIFIED_INTEGER_AMOUNT` to reconcile it. Zero is appropriate only with evidence of no charge. A later attempt still consumes a new daily count/reservation and must obey the remaining retry/cooldown limits. Source/recipe changes invalidate old evaluation evidence; recapture authorized evidence rather than resetting its state. `job-resume JOB_ID --reason ...` can resume a paused company job only with current unchanged approval, scope, pilot and deadline; it does not reset counters or retry failed pages.

## What the automated checks establish

Build verification on September 21, 2026 (Linux, Python 3.12, Django 5.2.17): **194 tests passed** (124 existing, 70 added); both real-process smoke scripts passed; Django system checks and migration drift checks passed. These are local results, not a claim of live Jev or PostgreSQL validation.

The test suite covers existing collector/discovery/automation behavior plus candidate retention, exact hashes/spans, expiry, changed source/recipe invalidation, shared channels, separate caches, shadow isolation, output contracts, token gates, durable accounting, retries, pause/resume, duplicate admission, stale leases, crash recovery, company routing/caps/manual approvals, staff authentication, CSRF and review audit. The process smoke additionally verifies file-backed SQLite contention and real worker/web restarts.

Not established by these tests: Jev classification accuracy or real billing, supported account/model availability, PostgreSQL contention, hardware power-loss durability, Windows behavior, live website recipe quality or browser/Ollama integration. No key, collected real HTML, runtime database or contact export is committed.

For rollback, stop both trial processes and keep the trial database with this branch. Point the original installation back to its untouched database/checkout. For a deliberate future production migration, first back up the database and `.env`; restoring that pre-upgrade backup is the reliable rollback. Do not run old code concurrently against this branch's database.
