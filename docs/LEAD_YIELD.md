# Lead yield work package 1

Branch `feat/lead-yield-v2`, based on `main` at `289ea86`. It fixes four losses reproduced by the September 24, 2026 audit. Everything was checked with fictional HTML and offline tests. It has not been deployed, and it does not measure yield on real sources.

## Behavior changes

**Fallback eligibility (JEV layered blocks).** `capture_page()` used to skip generic person blocks whenever any CSS row matched or any schema.org `Person` was parsed. Now the decision depends on *usable* candidates:

- A CSS row counts only if it names one plausible person and has bounded card evidence (the same test the row loop already applied).
- A structured `Person` counts only if it states an employer. Only those are captured.
- A block is skipped if it is, contains, or sits inside a captured card, or if it repeats a captured person's name. This prevents one person from being queued twice.

Unchanged: the `JEV_CAPTURE_ENABLED`/`JEV_LAYERED_BLOCKS_ENABLED` flags, the company-job exclusion, the header/nav/footer/hidden exclusions, the 6-block/2,400-character caps, and review-only judgments.

**Company evidence.** The company passage used to be the first 1,200 characters of page text. It now stays the page opening when the company name appears there, so existing spans are identical. Otherwise it is a 1,200-character window that starts up to 200 characters before the first real mention (locator `company-mention`, span key `company-mention`). A long navigation menu no longer stops person and block capture. If the page has no company evidence at all, capture still creates nothing, and no employer is invented.

**Visible contacts (CSS extraction and local recipe validation).**

- The default and generated contact selectors now read `a[href^="mailto:"]`/`a[href^="tel:"]` targets, `[itemprop=email|telephone]`, `.email`, `.phone` and `.telephone`. Visible text such as "Email: alex@…" is reduced to the address.
- When no selected email or phone is usable, a card-local fallback may use a single visible address from the card's evidence container. All of these must hold:
  - the card names exactly one person and is not inside, and does not use text from, `header`/`nav`/`footer`/`form`;
  - it has exactly one distinct candidate of that kind;
  - the candidate is not a generic inbox (`info@`, `sales@`, …) or a page header/footer/navigation contact;
  - the value does not also appear in another card.
- Explicit recipes that select plain-text phone numbers in other formats keep their previous behavior.
- Local validation (`automation.recipes.evaluate`) uses the same fields and keeps its own shared/global filter, which labels rejected values `shared_or_global_contact`.

**Ordinary crawl ordering.** `collect_links()` now collects every in-scope link, orders them with `discovery.ranking.link_priority()`, and only then spends the page allowance:

- The score reuses Discovery's target, editorial and product terms, plus profile/bio/leadership.
- Low scores go last but are never excluded. Campaign exclusions and Discovery's score floor are not applied to ordinary collection.
- Ties keep document order. Scope, depth, the page cap and external-candidate handling are unchanged.

## Rollout effects

- `leads.services.extraction.VERSION` changes from `0.3.0` to `0.4.0`. Each source's extraction signature changes, so unchanged pages are re-extracted once on their next crawl. This is local work only. Stored review notes and suppression decisions are preserved by the existing storage path, and the smoke tests confirm it.
- The extraction signature is part of the JEV evidence-document fingerprint. With `JEV_CAPTURE_ENABLED=1`, the next capture of each page creates a new evidence document. If `JEV_MODE=live`, that can queue one extra evaluation per page in the current weekly cache window. Newly eligible blocks and pages that previously stopped at the company check also add evaluations. All requests still go through the existing admission, daily allowance and reservations. No spending setting changed.
- Newly extracted visible contacts appear as new observations and leads for operator review, exactly like mailto/tel contacts. CSV export rules are unchanged.

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

Regression tests: `classification/tests/test_lead_yield_capture.py`, `leads/tests/test_visible_contacts.py`, `leads/tests/test_crawl_ordering.py`.

## Verification

```bash
.venv/bin/python manage.py test                       # 330 tests OK (293 existing + 37 new)
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
- Company identity is not yet optional in JEV capture. A page with no company evidence produces no candidates rather than candidates with an unresolved employer.
- Profile-only people without a contact, reviewed-block promotion, browser escalation, search diversification and new parsers are left to later work packages.
- Ordinary-crawl priority is keyword-based. It orders links but does not judge page quality.
