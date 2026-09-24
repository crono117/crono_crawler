# Lead yield work package 1

Branch `feat/lead-yield-v2`, based on `main` at `289ea86`. It fixes four losses reproduced by the September 24, 2026 audit. It also fixes the four findings from the independent PR #2 review (suppression lost when a lead gains an email, selected visible contacts bypassing safeguards, fallback coverage by name substring, and understated JEV re-evaluation) and the two storage findings from the follow-up review at `ab004b5` (held decisions lost on later contact changes, and continuity inferred from one surviving record among same-name cards). Everything was checked with fictional HTML and offline tests. It has not been deployed, and it does not measure yield on real sources.

## Behavior changes

**Fallback eligibility (JEV layered blocks).** `capture_page()` used to skip generic person blocks whenever any CSS row matched or any schema.org `Person` was parsed. Now the decision depends on *usable* candidates:

- A CSS row counts only if it names one plausible person and has bounded card evidence (the same test the row loop already applied).
- A structured `Person` counts only if it states an employer. Only those are captured.
- A block is skipped if it is, contains, or sits inside a captured card in the DOM, or if its own heading names exactly a captured person (after whitespace normalization and case folding). This prevents one person from being queued twice.
- A name mentioned elsewhere in a block does not count as coverage. For example, a biography that mentions a colleague, or "Joann Lee" next to a captured "Ann Lee", is still offered as a block.

Unchanged: the `JEV_CAPTURE_ENABLED`/`JEV_LAYERED_BLOCKS_ENABLED` flags, the company-job exclusion, the header/nav/footer/hidden exclusions, the 6-block/2,400-character caps, and review-only judgments.

**Company evidence.** The company passage used to be the first 1,200 characters of page text. It now stays the page opening when the company name appears there, so existing spans are identical. Otherwise it is a 1,200-character window that starts up to 200 characters before the first real mention (locator `company-mention`, span key `company-mention`). A long navigation menu no longer stops person and block capture. If the page has no company evidence at all, capture still creates nothing, and no employer is invented.

**Visible contacts (CSS extraction and local recipe validation).**

- The default contact selectors are still `a[href^="mailto:"]` and `a[href^="tel:"]`. Link targets and explicit recipe selections keep their existing behavior; their provenance is `selector`.
- A visible address may be inferred, with provenance `visible`, only when the card's contact selector is a default or generated link selector (either quote style) and it found no usable email or phone. The address can be wrapped in `.email`/`.phone`/`itemprop` elements, labelled ("Email: alex@…"), or bare text. All of these must hold:
  - the card names exactly one person under the name selector and is not inside, or itself, `header`/`nav`/`footer`/`form`;
  - the evidence container, excluding nested `header`/`nav`/`footer`/`form`, shows exactly one distinct address of that kind;
  - the address is not a generic inbox (`info@`, `sales@`, …) or a page header/footer/navigation contact;
  - the address does not also appear in another card.
- An explicit recipe selector, including an empty one, never triggers inference. An explicit `.email` still accepts an element that contains exactly one address, and still rejects one that contains several.
- Local validation (`automation.recipes.evaluate`) builds the same fields and keeps its own shared/global filter, which labels rejected values `shared_or_global_contact`. Ordinary extraction, local validation and generated recipes reach the same result for every safeguard fixture.
- Validated records carry `contact_provenance`, which is stored in each observation's `facts`.

**Stored lead continuity.** A lead's identity is its name plus email when it has a direct email, and otherwise its name, company, source and page. When re-extraction adds an email to a card that was stored without one, `save_records()` now continues the stored page-scoped lead, if continuity is provable:

- Continuity requires that CSS extraction matched exactly one card with that name, counted over every selector-matched row before evidence, contact and duplicate filtering (records carry this as `name_cards`). One surviving record among several same-name cards is not enough. Records without the count (local AI, structured recipes, or more than 500 rows) never prove continuity.
- It also requires a stored email that is empty or the same, and no conflicting stored phone.
- The existing lead is re-keyed and keeps its status and notes. Later crawls find it under the email identity.
- When continuity is not proven, the record is stored as a separate lead. No status, notes or historical contact fields are copied to it.

**Held decisions across contact changes.** The link between a reviewed person and later records is the page's observation history, which is kept when a lead is re-keyed and when it stops appearing:

- When a lead is newly tied to a page (created, or not previously observed there) and a lead with the same name observed on that source page is suppressed or rejected, the new lead inherits that status with a note naming the held lead. This covers an email that is lost, changed or becomes ambiguous after enrichment, a person first stored with an email who loses it, and an ambiguous same-name card.
- A held successor never becomes exportable without an operator decision. The note is added once; an operator who changes the successor's status is not overridden on later crawls.
- Returning to the original email finds the original lead again under its email identity.
- Positive review states (`reviewed`) are never copied. People with different names on the same page are unaffected, and people are never merged by name alone or by a shared phone or email. Generic inboxes keep the page-scoped identity.

**Ordinary crawl ordering.** `collect_links()` now collects every in-scope link, orders them with `discovery.ranking.link_priority()`, and only then spends the page allowance:

- The score reuses Discovery's target, editorial and product terms, plus profile/bio/leadership.
- Low scores go last but are never excluded. Campaign exclusions and Discovery's score floor are not applied to ordinary collection.
- Ties keep document order. Scope, depth, the page cap and external-candidate handling are unchanged.

## Rollout effects

- `leads.services.extraction.VERSION` changes from `0.3.0` to `0.4.0`. Each source's extraction signature changes, so unchanged pages are re-extracted once on their next crawl. This is local work. Leads that gain an email on a unique card keep their review state as described in "Stored lead continuity" above; unmatched successors of suppressed or rejected leads are held.
- The extraction signature is part of the JEV evidence-document fingerprint, and that fingerprint is part of every evaluation cache key. With `JEV_CAPTURE_ENABLED=1`, the next capture of each unchanged page therefore re-creates **all** of its evaluations once: one company evaluation, one per captured person, one per employer named in a card, and the page/block evaluation when blocks exist. Batching by the token limit can split these further.
- Measured with the mock provider on an unchanged page with three CSS people: the first capture created 4 evaluations. After only the version change, the next capture created 4 new evaluations. Repeating it created 0 more. Live charges were not measured.
- Newly eligible blocks, and pages that previously stopped at the company check, add evaluations too. All requests still go through the existing admission, daily allowance and reservations. No spending setting changed. With `JEV_MODE=live`, expect roughly one re-evaluation of every captured page's company, people and blocks within the first crawl cycle, spread over days if the daily allowance is reached.
- Newly extracted visible contacts appear as observations and leads for operator review, exactly like mailto/tel contacts. CSV export still excludes suppressed and rejected leads.

## Before/after (fictional fixtures, sockets blocked)

Measured with the same script on `289ea86` and on this branch:

| Case | `main` @ 289ea86 | This branch |
| --- | --- | --- |
| Company heading + nonstandard employee section | 2 evaluations, 1 block question | unchanged |
| + junk `.team-member` "Team" row | 1 evaluation, 0 block questions | 2 evaluations, 1 block question |
| + employer-less JSON-LD author (packs on) | 1 evaluation, 0 block questions | 2 evaluations, 1 block question |
| Long navigation before the company heading (block page) | 0 evaluations | 2 evaluations, 1 block question |
| Long navigation before the company heading (CSS card) | 0 evaluations, 0 people | 2 evaluations, 1 person |
| Valid CSS card + nonstandard employee section | 2 evaluations, 0 block questions | 3 evaluations, 1 block question (no duplicate for the CSS person) |
| Profile link, no contact (out of scope) | 1 evaluation, 0 blocks | unchanged |
| No company evidence anywhere | nothing captured | unchanged |
| `<span class="email">` in a card | 0 records | 1 record |
| Plain-text phone in a card | 0 records | 1 record, exact text `(555) 010-1234` |
| Two different visible emails in one card | 0 records | 0 records (ambiguous) |
| Footer contact repeated inside a card | 0 records | 0 records |
| Switchboard phone printed in two cards | 0 records | 2 records with their own emails, no phone |
| Best of the generated recipes on the visible-email page | 0 accepted | 1 accepted |
| Ordinary crawl, `max_pages=3`, team link last | home, 2 product pages | home, team, 1 product page |

PR #2 review fixtures, using the review's harness. The "first PR head" column is `d5bc6a9`; this branch column is after the review fixes. The ordinary-extraction result is shown; local validation and generated recipes match it on this branch.

| Review case | `main` | First PR head | This branch |
| --- | --- | --- | --- |
| `.email` element containing two addresses | 0 records | first address assigned | 0 records |
| Second person's heading, then their `.email` | 0 records | Jordan's email assigned to Alex | 0 records |
| `.email` inside a nested footer | 0 records | footer address assigned | 0 records |
| `.email` containing `info@` | 0 records | kept, marked shared | 0 records |
| Same `.phone` switchboard in two cards | 0 records | both phones kept, marked shared | own emails only, phones dropped |
| Suppressed phone-only lead re-extracted with an email | n/a | new exportable `new` lead | same lead, still suppressed with notes; export empty |
| Joann Lee block next to a captured Ann Lee card | block dropped | block dropped | 1 block question for Joann |
| Version change on a 3-person page (mock) | n/a | 4 new evaluations | 4 new evaluations (now documented) |

Follow-up review fixtures at `ab004b5`, using that review's multi-crawl harness. Each held case stores a phone-only lead with `main`'s extractor, sets the status, re-extracts to add the email, then crawls the change. Results were identical for `suppressed` and `rejected`.

| Follow-up case | `ab004b5` | This branch |
| --- | --- | --- |
| Held lead's email disappears | second lead `new`, 1 exported row | second lead held with note; export empty |
| Held lead's email changes to `alex.new@` | second lead `new`, 1 exported row | second lead held with note; export empty |
| Held lead's email becomes ambiguous | second lead `new`, 1 exported row | second lead held with note; export empty |
| Two selected same-name cards, one accepted record, original `reviewed` | original lead takes the other card's email, keeps old phone, `reviewed` notes | original keeps phone, `reviewed` and notes; other card is a separate `new` lead with no notes |

Regression tests: `classification/tests/test_lead_yield_capture.py`, `leads/tests/test_visible_contacts.py`, `leads/tests/test_contact_continuity.py`, `leads/tests/test_crawl_ordering.py`.

## Verification

```bash
.venv/bin/python manage.py test                       # 367 tests OK (293 existing + 74 new)
.venv/bin/python manage.py check                      # no issues
.venv/bin/python manage.py makemigrations --check --dry-run   # no changes
.venv/bin/python manage.py benchmark_extraction --assert-fixtures  # packs 10/10, 0 extra; baseline 0/10
.venv/bin/python scripts/smoke_local.py               # PASS
.venv/bin/python scripts/smoke_extraction.py          # PASS
.venv/bin/python scripts/smoke_jev.py                 # PASS
.venv/bin/python scripts/run_smoke_bulk_import.py     # PASS (isolated copy, no .env)
```

## Remaining limitations

- The fixtures prove specific behavior. They do not measure how often these layouts occur on real sources. Run the annotated replay from the audit before and after deployment.
- The visible-contact fallback recognizes US-style phone numbers and `+` international numbers. Other plain-text formats need an explicit phone selector.
- Visible contacts are found only inside matched person cards. Pages with no card selector match still depend on recipes, structured data, or JEV blocks.
- A lead that loses or changes its email gets a new identity. If the original was suppressed or rejected, the successor is held; otherwise it is a separate `new` lead for review, and the two are not merged automatically.
- Held decisions follow a person only on the same source page. A person who moves to another page or source is not linked.
- A new lead is held when a same-name lead on that page is held, even if they are different people. An operator releases it by changing its status.
- The one-person check uses the recipe's name selector. A second person named only in an element outside that selector is not detected.
- Local AI and structured-recipe records carry no card count, so they never continue a page-scoped lead when an email is added; a held original still holds the successor.
- Company identity is not yet optional in JEV capture. A page with no company evidence produces no candidates rather than candidates with an unresolved employer.
- Profile-only people without a contact, reviewed-block promotion, browser escalation, search diversification and new parsers are left to later work packages.
- Ordinary-crawl priority is keyword-based. It orders links but does not judge page quality.
