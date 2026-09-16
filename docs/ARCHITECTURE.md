# Architecture

## Components

Django serves a session-authenticated, CSRF-protected staff console. Server-rendered templates and local static files keep installation simple. No JavaScript build step, hosted database, message broker or cloud AI account is required.

SQLite is the local database, with WAL mode, a busy timeout and immediate write transactions. PostgreSQL uses the same Django models. A database-backed scheduler/queue avoids adding Redis for the single-worker pilot.

The collector is a separate management command. A lease prevents two workers from intentionally claiming pages at once. The heartbeat expires after ten minutes; stale workers' processing pages return to the queue when a replacement obtains the lease. Saving observations, snapshots, discovered jobs and run counts happens in one transaction. A page repeated after a crash upserts the same identities and evidence.

## Collection flow

1. The scheduler queues an active, approved, due source if it has no open run.
2. A page job waits for its origin's rate limit. Robots rules are fetched and cached for 24 hours. Missing robots (404/410) allows fetching; failed/blocked robots checks do not silently become permission.
3. HTML transport pins a validated public DNS answer for the socket connection, keeps hostname certificate validation, bounds response bodies and validates each redirect. The browser transport routes requests through that transport and checks navigations against source scope and robots.
4. The page content hash and extraction settings determine whether extraction is needed.
5. CSS extraction selects named person cards; optional Ollama returns candidate fields plus a contiguous evidence passage. Validation rejects unsupported fields. Keyword service tagging is deterministic in both modes.
6. The storage layer preserves observations separately from the contact summary and from review decisions.
7. In-scope links become bounded page jobs. External domains become review candidates. No candidate automatically expands the crawl scope.

Transient HTTP failures retry with exponential backoff and a bounded Retry-After value, up to four failed attempts per page. HTTP 401/403, disallowed robots, and recognized access challenges pause the source. A collector or extractor configuration error becomes a failed page with a visible message. The operator can fix the source and start a fresh run.

## Data model

`Source` holds scope, review, scheduling, and extraction settings. `Run` groups `PageJob` checkpoints. `DomainState` holds robots and throttle state. `WorkerLease` coordinates the single collector. `Lead` stores the current summary and review. `Observation` stores the facts/evidence for one lead on one source page. `PageSnapshot` stores hashes and last successful checks. `SourceCandidate` is the queue of unapproved external domains.

The source category is operator context. Person tags come from the selected person passage; page tags come from the full page. A too-broad CSS selector can produce overly broad attribution, so each real source needs a recipe/evidence review.

CSS recipes can narrow evidence to a descendant of each selected person card. Per-page messages report matched-card counts and rejection reasons without storing rejected contact fields. `manage.py inspect_recipe` previews saved HTML locally without requests or database writes. Dashboard recipe-review warnings are derived from the latest completed run per source/campaign and also cover pre-existing history; zero new leads with nonzero contact observations is a healthy refresh.

## Known boundaries

The discovery app extends this pipeline with `Campaign`, `DiscoveryRun`, `DiscoveredURL`, `DiscoveryJob` and `DailyUsage`. One collector lease covers both job queues; the worker alternates queue preference and rotates runnable campaigns. Discovery uses the same transport, robots preparation, source scope, throttles, extraction/evidence validation and lead storage. Its additive migration leaves the existing lead/source tables unchanged. Full workflow and limits: [Discovery](DISCOVERY.md).

- Exactly one worker per database; this is not a distributed crawling fleet.
- Scope is an exact HTTP(S) origin plus path prefixes. No wildcard domain permissions.
- Public DNS addresses are required for collected sites. Localhost is allowed only for the administrator-configured Ollama endpoint and the fixed offline demo never uses the network.
- The browser path does not forward session cookies or execute POST APIs. Some JS sites will not render fully; 1.5 seconds of post-DOM settling is only a pilot default.
- Transport honors an identity-encoding request; a server that insists on compression is rejected rather than risking an unbounded decompression step.
- DNS/TLS/network operations can fail; the network path is not a substitute for isolating the collector from sensitive infrastructure on a shared server.
- Evidence is retained until the operator administers retention/deletion. There is no automatic retention period or erasure workflow in this pilot.
- No full raw-page archive, source-change notification, or automatic crawling of search engines.
- Conservative deduplication does not fully resolve identity across job moves, contact changes, or shared addresses. Review state belongs to a specific stored identity.
- AI's evidence match limits fabrication but does not prove the passage attributes every field correctly; reviews remain necessary.
- URL-form DNS checks are synchronous and can delay a source form save on a slow resolver.

## Next implementation priorities

1. Validate approved representative websites and add tested source recipes.
2. Exercise Chromium and local Ollama on the user's actual hardware.
3. Extend the zero-contact health warnings with freshness filters once a real refresh cadence is established.
4. Add explicit user roles, suppression matching across identity changes, retention/deletion controls, and an operator audit trail before wider team use.
5. Add a distributed queue only when measured throughput justifies multiple workers.
