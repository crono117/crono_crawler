# Autonomous site setup

Branch: `feat/discovery-v1`. This extends Discovery in the existing Python app and single worker. No hosted AI, Ollama, new background service, or paid search account is required.

Enable one **campaign policy**, rather than approving every new website. A qualifying discovery URL becomes a Source authorized by that policy. It can progress through probing, recipe validation, release and a canary without an operator click. The URL score authorizes entry into this bounded process; it cannot bypass recipe or contact-evidence checks.

Existing installations keep their previous behavior until a policy is enabled. Existing sources remain explicitly **operator-configured rules** sources. They are not silently converted or given wider scopes.

## Operator controls

1. Open **Discovery → your campaign → Automation policy**.
2. Enable automatic setup. Set the permitted paths, additional excluded domains, request delay, daily new-site limit and score thresholds. Save the policy.
3. Start/resume the campaign. Eligible URLs already pending are reconsidered by the worker, including after the daily new-site allowance resets. Newly discovered qualifying URLs use the same policy.
4. Open **Site automation** to see setup progress, validation results, versions and exceptions. A passing site joins normal collection automatically: its approved pages enter the campaign's open run right away, or the next run if the campaign is paused.
5. To evaluate an existing source such as Host Merchant, use **Set up recipe automatically** next to that source on the campaign page. This retains its exact origin, paths, category, restrictions and delay, cancels its unfinished ordinary run, and pauses collection until setup passes.

The worker leaves an origin with an existing Source alone during automatic onboarding. This preserves previously configured or revoked approvals; opt an existing source into setup explicitly. A policy never expands an existing source's paths. Edit its source scope deliberately if more paths are needed.

Saving policy settings pauses existing automatic setups for revalidation. Use **Probe / retry setup** for sources you want to resume under the changed policy. This also applies when re-enabling a disabled policy. Changing campaign filters retains their existing save-and-pause behavior: resume the campaign afterward.

## Defaults and bounds

| Setting | Default | Effect |
| --- | --- | --- |
| New-site discovery score | 70 | Relevant URL plus campaign business/role signals; configurable 35–100 |
| Recipe readiness | 85 | Supported individual profiles can pass; 90 requires repeated cards |
| New sites per UTC day | 3 | Per campaign; configurable 1–25 |
| Probe | At most 3 pages, depth 1 | Configurable 1–5 pages |
| Canary | At most 5 pages, depth 1 | Configurable 1–10 pages; no search or external expansion |
| Delay | At least 5 seconds | New sites only; robots and existing per-origin throttle can require longer |
| Scheduled re-probe | 7 days | Configurable 1–90 days |
| Private HTML lifetime | 24 hours | Snapshots become unreadable to validation after expiry |

Default path prefixes cover team, staff, people, sales, agents, representatives, reps, partners, contact, about, leadership, executive-team and independent-sales pages. Inspect the actual list in the policy. An optional **exact homepage** permission permits `/` without permitting every other path. A literal `/` in the prefix list grants the whole origin and should be a deliberate choice. Origins include scheme and port: redirects to a different origin remain disallowed, even between `www` and bare hostnames.

**`require_page_intent`** (default: on) additionally requires the candidate URL/label to already look like a team, staff or sales-role page before automatic setup, on top of the new-site discovery score. Turning it off lets a bare company homepage qualify too — in practice, many collector-discovered links are a company's root URL rather than a URL that already names a team/staff page. The unchanged safety gates still apply regardless of this setting: `denied_domains`, in-scope/robots enforcement, and a negative or campaign-excluded score are always disqualifying. Everything after authorization is identical either way — the same bounded probe, recipe validation and canary decide whether the site actually yields anything; turning this off does not skip or weaken that pipeline, it only widens which URLs are offered to it.

Facebook, LinkedIn, X/Twitter, Instagram, TikTok, YouTube and Reddit remain excluded from automatic setup. Add other domains in the policy. Query-bearing URLs do not initiate automatic site setup. Campaign exclusions also apply. An operator's origin dismissal prevents automatic onboarding; a URL dismissal prevents that URL from being retried. Automatically filtered product URLs do not become operator domain dismissals.

The policy's new-site score is separate from the campaign's page-priority threshold. A contact-focused campaign can retain a page threshold near 35 while requiring 70 for a previously unknown site. The campaign's daily request budget is shared by discovery, probing and canaries. Robots checks, retries and fetch attempts consume it; it survives restarts. The offline fixtures do not use network quota.

Policy authorization is an operating decision, not a declaration that a human reviewed every site's terms. It is stored as **Campaign policy**, separate from **Operator review**. Explicit collection-prohibition phrases trigger review, but that narrow detector is not a comprehensive terms interpreter. Scores cannot establish permission for every reuse of published data.

## Pipeline and persistence

`SiteAutomationJob` records the source, campaign policy snapshot, source-scope hash, generation, lease, state, message and next probe time. `ProbePage` checkpoints each bounded fetch. `RecipeVersion` retains candidate selectors and validation results; releases never replace earlier version records. `AutomationEvent` records stage transitions.

The state sequence is:

```text
probe_queued → probing → probe_complete → recipe_queued → recipe_testing
→ recipe_ready → recipe_released → canary → active
```

Any stage can pause or fail. The source's ordinary scheduler stays off while automatic setup owns it, including after activation: normal collection uses its Discovery campaign. Regular source collection cannot bypass the setup gate. There is still exactly one worker per database; setup is another queue under its existing lease, not another daemon.

The worker rechecks authorization before fetching and before committing results. Source edits, a changed policy, revocation, a newer setup generation or a replaced worker lease invalidate old work. Transient HTTP failures retry up to four attempts with persisted backoff. Budget exhaustion waits until the next UTC day. A restart resumes the persisted stage. An abrupt crash retains the existing ten-minute worker-lease timeout.

## Probe and recon

The probe uses static HTML and the existing public-DNS, pinned-connection, redirect, robots, size and delay controls. It does not execute site JavaScript, submit forms, log in, fetch social profiles, rotate proxies or solve challenges. A block or disallowed robots policy pauses the site. It does not seek an alternate access route.

Recon contains final URL and redirect metadata, response size/type, declared encoding, content hashes, robots policy hash and sitemap references, framework hints, a JavaScript-heavy hint, candidate container selectors and aggregate name/role/contact counts. Probe sitemaps are recorded, not fetched. Links can queue only relevant pages in the same approved origin/path scope, within the depth and page bounds. Links with direct contact-page intent (team, staff, people, bio, leadership, reps) are probed first. If the probe budget has room left, in-scope About/company pages and then Contact pages follow, because many small businesses list their people there rather than on a `/team` page. That fallback is only a probe-ordering rule inside an already-authorized site: it never counts as contact-page intent for authorizing a site or queueing an ordinary discovery page, and editorial, product and document/social/scheduling links are still excluded.

Raw HTML goes to `DATA_DIR/automation-private`, with private directory/file permissions. Neither the recon API nor the dashboard exposes that HTML. Recon excludes extracted names, emails, phone values and page text; URLs themselves can still identify individual public pages. Snapshots expire after 24 hours and the running worker deletes expired files, including orphaned files from interrupted writes. When the worker is stopped, deletion waits for restart, but expired snapshots cannot be used by validation. Disk encryption is the host operator's responsibility.

Accepted contacts, evidence and review decisions remain in the existing local database. Their retention is independent of the short HTML lifetime. There is no remote raw-HTML upload feature or automatic contact upload to a recipe service.

## Deterministic recipes and validation

Candidates combine common card selectors, recognized semantic classes such as `.agent-tile`, and bounded name/title selector alternatives. This is HTML analysis, not an LLM. Up to 65 recipes are tested per generation and the best five become version records. A supplied or previously released source recipe is also considered.

Field selectors are relative to a person card. `evidence` may select that card itself or a descendant. For example:

```json
{
  "row": ".team-member",
  "name": ".name",
  "title": ".title",
  "email": "a[href^='mailto:']",
  "phone": "a[href^='tel:']",
  "evidence": ".team-member"
}
```

Validation uses saved local HTML. It requires a supported name, published email or phone, contained evidence, and a published title when the source requires sales roles. It rejects ambiguous multi-person containers, unsupported fields, generic/global contact channels and channels shared by different names. A valid individual channel can survive removal of a shared switchboard. Duplicate cards are counted and penalized. Existing contact validation, suppression and formula-safe CSV export remain in force.

Readiness awards 20 for repeated cards (15 for a single card), 20 for at least 80% sales-role co-location, 25 for accepted contact values, and 25 for evidence without hard validation errors. Hard errors or hitting the 500-card page bound incur a further penalty. The maximum is 90. This is an explainable heuristic, **not a calibrated probability or a measured false-positive rate**. Evidence checks cannot prove mailbox ownership or perfectly resolve identity.

Diagnostics distinguish no matching cards, cards without usable contacts, invalid contacts/evidence, accepted contacts, and unavailable/blocked pages. Zero contacts pauses for recipe review; it is not represented as a successful release. A page can be highly relevant yet publish no suitable individual contact details. That remains a likely outcome for executive pages.

### Site assessments

Every site that reaches recipe validation, and every site that pauses for a reason about the site itself (access challenge, robots, collection restriction, out-of-scope redirect, unavailable pages), gets one `SiteAssessment` per job generation: an outcome (`contacts_found`, `people_without_personal_contacts`, `contacts_rejected`, `cards_without_people`, `no_people_listed`, or a failure code), reason codes, "future options" naming the capability that could unlock the site later (for example `shared_contact_leads`, `non_sales_people_listed`, `browser_collector`, `platform_recipe_pack`, `operator_confirmed_employer`), aggregate facts per page, and a one-line summary. Because an assessment describes what the site publishes, each page uses the richest observation across all tried recipe variants (preferring specific card classes and structured data over generic catch-alls), not only the winning recipe.

Assessments hold counts, page kinds, selector names, framework hints and reason codes only — never names, emails or phone numbers. Pauses caused by our own policy/scope edits or operator actions are not recorded as site findings; an operator's closing note on an assessed site is appended as a note and does not overwrite the observed outcome. Assessments are advisory: they never authorize a source, release a recipe or create a contact.

`manage.py site_assessments` prints outcome/reason/future-option counts and recent summaries. `--backfill` creates assessments for existing jobs without network requests, re-evaluating stored probe HTML while it is still retained and otherwise using saved validation stats; `--refresh` recomputes current-generation assessments. The Automation home page shows the same breakdown.

An operator can pause a job and submit a candidate recipe on its detail page. It still goes through local validation and the same release threshold. If snapshots have expired, run a fresh probe first. A human correction does not write asserted contacts or bypass the canary.

## Release, health and rollback

A passing candidate is released only to the canary. The canary refetches bounded pages using existing delays and budgets. It checks accepted counts, container structure, evidence errors and the observed robots policy. No candidate/canary contacts are saved until the entire canary passes. Then the version becomes `known_good`, supported contacts are saved and normal campaign collection becomes eligible. In the same transaction, the site's already-approved in-scope URLs are queued into the campaign's open discovery run through the ordinary queueing gates (approval, score and direct intent, collection permission, depth, per-run page limit and dedup). URLs the canary just checked are skipped. A paused or finished campaign picks them up on its next start, as before. The site's event history records how many pages were handed off.

Normal managed collection repeats strict validation before replacing observations. A sudden zero result on a previously productive page, a greater-than-half count loss, container drift, evidence errors, duplication or a rejection spike pauses collection and queues a bounded re-probe. Prior observations and operator decisions survive. If rebuilding is ambiguous, the source stays paused for review.

An access block or an observed robots-policy change pauses for operator review without an automatic access retry. Robots checks retain the existing 24-hour cache; change detection happens when a new policy is observed, not instantly when the remote file changes.

On degradation, the current recipe is marked paused and the last known-good version is restored when available. Restoration does not resume crawling by itself; setup must pass again. Without an older good version, the source remains paused. Version selectors and original validation results are retained; canary results are recorded separately. Editing a recipe during an in-flight request is not overwritten by rollback.

## Optional coordinator API

The complete local loop runs without an external server. This release also provides a campaign-scoped metadata/proposal API for a trusted coordinator. It does **not** deploy or validate a separate cloud coordinator/desktop-client fleet.

Create a credential only when using that integration:

```bash
.venv/bin/python manage.py create_coordinator_client --campaign 1 --name home-coordinator
```

The command prints a random bearer token once. Store it privately; only its hash is stored in the database. Send it as `Authorization: Bearer <token>`. Disable the corresponding Coordinator client in Django admin to revoke it.

| Endpoint | Behavior |
| --- | --- |
| `GET /automation/api/jobs/` | First 100 jobs in the credential's campaign |
| `GET /automation/api/jobs/ID/recon/` | Recon metadata only |
| `POST /automation/api/jobs/ID/candidate/` | Accepts only `recipe`, `generation`, `scope_hash`; local validation required |
| `GET /automation/api/jobs/ID/bundle/` | Active known-good recipe in a signed, time-limited bundle |

Candidate bodies are limited to 16 KB and five external proposals per generation. Credentials cannot read other campaigns or assert contacts. Bundles bind source, origin, scope hash, version and validation metadata. `automation.coordination.verify_bundle` checks the one-hour expiry, signature and expected scope. Signing uses a dedicated per-client shared secret, **HMAC rather than asymmetric signing**; the trusted client knows that secret. The Django application secret is not shared.

Non-loopback API access requires Django to recognize HTTPS. Forwarded-protocol headers alone are not trusted. For the private local pilot, use a localhost connection or SSH tunnel. A remote deployment must deliberately configure TLS and its trusted proxy boundary; do not simply forward the development server's port. No API token is needed for the ordinary staff dashboard or local automatic worker.

## Hermes upgrade handoff

Use the existing installation and preserve local changes, accounts and data. Do not start a second worker or replace the database with the offline demo.

1. Record the deployed commit and inspect `git status`. Stop the existing web/worker launcher or services. Back up the actual database, `.env` and `DATA_DIR` outside the repository. With SQLite stopped, preserve the whole data directory, including WAL/SHM files. Use the appropriate database backup if running PostgreSQL.
2. Fetch `origin` and fast-forward `feat/discovery-v1` to the new remote commit. If local changes/divergence prevent this, preserve them and use a separate checkout for comparison; do not reset them away.
3. Run:

   ```bash
   python3 setup.py --no-user
   .venv/bin/python manage.py check
   .venv/bin/python manage.py test
   .venv/bin/python scripts/smoke_local.py
   ```

4. Restart the existing launcher/services with one worker. Enable the chosen real campaign's Automation policy, keep its exclusions and daily cap, and resume it. For Host Merchant, opt its existing source into automatic setup from the campaign page; do not widen its current paths silently.
5. Inspect **Site automation**. Report state, generation, score, matched/accepted/rejected counts and pause reasons. Verify evidence on any accepted real contact. Do not report lead yield as improved if the source still has no supported person-level contact details.

The migration adds source mode/authorization fields, dismissal provenance and automation tables. Old dismissed URLs are conservatively treated as origin dismissals because the previous schema did not distinguish operator dismissals from filtering. Review those deliberately if they should become eligible again; the migration does not revive them automatically.

For an optional fictional walkthrough after upgrading, run `manage.py init_automation_demo` with the virtualenv Python. The existing worker should activate the fixed `automation.example.test` source and add two fictional people, Robin Autonomy and Morgan Pipeline. Repeating it should not create duplicates. Pause its campaign when finished. The smoke test already exercises this in an isolated temporary database.

**Code rollback:** older releases do not understand automatic setup gates. Before returning to older code, stop collection, pause affected campaigns and revoke approval on automatically managed sources using the current release. Leave those sources disabled after rollback. Do not merely check out older code with their approved campaign memberships still active. Alternatively, restore the pre-upgrade backup deliberately, accounting for any data collected since that backup. No destructive reverse migration is needed for the normal upgrade.

See [validation results](VALIDATION.md) for tested behavior and remaining deployment/real-source gaps.

## Crawler lab (activity feed)

The worker appends one JSON line per step to `DATA_DIR/activity.jsonl` (override with `ACTIVITY_LOG`, `off` disables it; always off under `manage.py test`): lane picks, every HTTP request with status/bytes/timing or error, robots checks, throttle waits, results with contact counts, automation state changes, Jev evaluation outcomes, and a once-a-minute idle note saying when queued work is next due. Lines hold URLs, statuses and job messages only, never page content, names, emails or phones. The file is capped at about 1 MB and writing is best-effort, so it can never fail worker work. The web process only reads it.

**Crawler lab** (`/automation/lab/`, staff only) acts the feed out in a top-down room with one crew member per lane (collector, discovery, site setup, Jev) and shows a console per lane plus the raw feed. There is still exactly one worker process; the lab shows which lane it is currently serving. The optional Neo Zero tileset (itch.io, free version non-commercial with credit, no redistribution) is never committed: unzip your own download into the git-ignored `static/local_assets/neo-zero/` and map frames in `static/lab/lab-frames.js`; without it the lab uses drawn shapes. Likewise, the GameDev Market sci-fi robots 16×16 pack (standard GameDev Market license: use in your project, no redistribution) goes in `static/local_assets/robots/`; `LAB_ROBOTS` in the same file sets the frame grid (rows per direction, idle/walk columns) and lane-to-sheet picks. Without it the lab uses the Kenney CC0 crew sprite. A flattened sheet (JPEG with a painted checkerboard, uneven cells, labels over the art) can be converted with `python3 scripts/prepare_lab_sprite.py static/local_assets/robots/<file>.jpeg`, which writes a transparent, feet-aligned PNG grid next to it. Bots move tile by tile on a 48px grid along a breadth-first path around desks, props and each other; their collision box is the bottom half of the sprite, anchored at the feet, and sets draw order. Open the lab with `?footprints=1` to see the boxes.

### Operator-confirmed employer for structured profiles

Structured Person data without `worksFor` is rejected (`missing_employer`); the employer is never inferred, and a source's `company` field is not treated as the employer of structured profiles. On a source's page an operator can confirm who the listed people work for ("Employer of listed people"). That sets `Source.structured_employer`, records who confirmed it and when in the review notes, and re-queues site setup for automatic sources. Profiles without `worksFor` then use it, and each contact's evidence carries `[Employer confirmed by operator for this source: …]`. A stated `worksFor` always wins. The confirmation is part of the extraction signature, so unchanged pages are re-read once.

### Shared-contact leads (opt-in)

With `SHARED_CONTACT_LEADS=1`, a card that names a person with an explicit sales title but shows no usable personal email or phone may be stored with the site's own shared company contact from the same page: a generic inbox (info@, sales@, …) on the site's own domain, or else an email/phone in the page header/footer/navigation. Third-party addresses are never used. The lead's `contact_scope` is `shared` ("Shared business attribution" in the console and CSV), and its evidence carries `[Shared company contact shown on this page (…), not this person's own: …]`. A personal contact on the card always wins. Structured-data profiles are not paired this way. The setting is part of the extraction signature, so unchanged pages are re-read once when it changes.

Discovery also keeps the shared daily budget for sites that can still yield: a site with 10+ fetched pages and no stored contact gets 5 pages per run (seeds exempt), and each source gets at most 5 sitemap files per run. Skipped jobs make no request and say why; the next run gets a fresh allowance.

### Placeholder, template and decoy contacts

Validation drops contact values that are never a real person's: theme demo content (placeholder names such as "John Doe", "Your Name", "Team Member", lorem-ipsum cards; mailboxes such as `yourname@`/`john.doe@`; domains such as `yourdomain.com`), framework artifacts that look like emails (`logo@2x.png`, Sentry/Wix addresses), template phone numbers (repeated or sequential digits), and — on real sites only — reserved example domains and 555-01xx/555-5555 numbers (fixture sites on reserved hosts use those on purpose). Elements hidden from visitors (`hidden`, inline `display:none`/`visibility:hidden`) are removed before extraction, so honeypot addresses are never collected. Rejections are counted as `placeholder_name`/`placeholder_contact` and surface in site assessments as `placeholder_or_decoy_contacts`. Cloudflare email protection is counted (`protected_emails`, reason `emails_hidden_by_protection`) but never decoded: it exists to keep addresses from crawlers.
