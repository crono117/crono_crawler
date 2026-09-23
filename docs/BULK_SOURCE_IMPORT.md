# Bulk-upload sources with JSON

This format adds **website source configurations**, not contacts. Each new source is created **paused and unapproved**. Uploading a list never starts a crawl, grants source permission, calls a paid provider, or creates a discovery campaign.

## Quick start

1. Open **Sources → Import JSON** in Lead Console after signing in with a staff account.
2. Download the **example JSON** from that page, or copy `examples/bulk-sources.json` from the repository.
3. Replace the fictional example names/domains with the exact public starting pages you want to review. Prefer a specific team, sales, contact or directory page over a homepage.
4. Save as a `.json` file encoded in **UTF-8**. UTF-8 with a BOM is accepted too.
5. Select the file and choose **Preview**. Check the normalized URLs, path scope, limits and duplicate results. Preview does not create sources or fetch websites.
6. Confirm the import. Only new URLs are added; existing records are not edited. Review the final created/skipped counts and row results.
7. Open each new source, inspect its extraction settings and permitted scope, review source terms/restrictions, add the review note and approve through **Edit**. Start collection separately only when appropriate. Public visibility alone is not blanket permission for reuse.

The preview is signed, belongs to the staff account that uploaded it, and expires after **15 minutes**. If expired, upload the file and preview again. A second confirmation is safe: URLs already imported are skipped, not duplicated or reactivated.

## Smallest valid file

```json
{
  "schema_version": 1,
  "sources": [
    {
      "name": "Example Payments — Sales Team",
      "url": "https://payments.example.org/team/"
    },
    {
      "name": "Example POS — Contact Page",
      "url": "https://pos.example.org/contact/",
      "category": "pos"
    }
  ]
}
```

**These are fictional placeholders, not researched or approved collection targets. Replace them before use.**

## Example with all supported source fields

```json
{
  "schema_version": 1,
  "sources": [
    {
      "name": "Example Payments — Sales Team",
      "url": "https://payments.example.org/team/",
      "company": "Example Payments",
      "category": "merchant_services",
      "allowed_paths": ["/team/"],
      "max_pages": 5,
      "max_depth": 1,
      "delay_seconds": 5,
      "interval_hours": 168,
      "follow_links": false,
      "discover_external": false
    }
  ]
}
```

## File-level rules

| Field or limit | Requirement |
|---|---|
| `schema_version` | Required JSON integer `1`; not `"1"`, `1.0` or `true` |
| `sources` | Required array of **1–100** source objects |
| File size | At most **256 KiB / 262144 bytes**, including any BOM |
| Filename | Must end in `.json` (case-insensitive) |
| Encoding | UTF-8; optional UTF-8 BOM |
| JSON syntax | Double quotes, no comments/trailing commas, no duplicate keys, no `NaN`/`Infinity` |
| Extra fields | Rejected, both at the top level and inside source objects |

Split larger batches into multiple files. You do not need a campaign ID. This browser upload is distinct from the existing command-line discovery-candidate metadata importer, which uses a different format and creates candidates rather than Sources.

## Source fields

Only `name` and `url` are required. Omitted optional fields receive the defaults below.

| Field | JSON type | Default | Valid values / meaning |
|---|---|---|---|
| `name` | string | Required | Nonempty display name, at most 160 characters before trimming. Leading/trailing ordinary spaces are trimmed; controls and other whitespace are rejected. |
| `url` | string | Required | At most 1500 characters. A public HTTP(S) content-page URL with no credentials, whitespace, backslashes, control characters or nonstandard ports. |
| `company` | string | `""` | At most 160 characters before trimming; ordinary outer spaces are trimmed, controls and other whitespace rejected. Operator-supplied context, not a verified fact about an extracted person. |
| `category` | string | `"merchant_services"` | One exact category key from the table below. |
| `allowed_paths` | array of strings | Decoded path of the starting URL, or `["/"]` for a homepage | 1–10 path prefixes, each 1–1500 characters. The starting URL must be covered. See scope rules below. |
| `max_pages` | integer | `5` | 1–500 pages per run, after source approval and activation. |
| `max_depth` | integer | `1` | 0–5 link levels; applies when following links is enabled. |
| `delay_seconds` | integer | `5` | 2–3600 seconds between requests; other transport/robots limits still apply. |
| `interval_hours` | integer | `168` | 1–8760 hours between scheduled runs after activation. |
| `follow_links` | boolean | `false` | If later activated, follow only permitted in-scope links within the page/depth limits. |
| `discover_external` | boolean | `false` | If later activated, save external links as review candidates; does not authorize fetching new websites. |

Write integers as `5`, not `"5"` or `5.0`, and booleans as `true` / `false`, not `"true"`, `"false"`, `1` or `0`. Do not use `null` for optional values; omit the field instead.

### Category keys

| Key | Label |
|---|---|
| `merchant_services` | Merchant services |
| `pos` | Point of sale |
| `payroll` | Payroll & HR |
| `funding` | Business funding |
| `telecom` | Business telecom |
| `it_services` | IT & managed services |
| `insurance` | Business insurance |
| `other` | Other / unclassified |

### URL normalization and safety

The importer validates raw URLs before normalizing them using the application's existing source-URL rules:

- HTTP(S) scheme and hostname are normalized; an empty path becomes `/`, default ports and fragments are removed.
- The query string is retained, including its order and tracking parameters. A distinct query string is a distinct source URL.
- Do not put passwords, access tokens, private session links or other secrets in the file. URL credentials are rejected; rejected raw URL strings are not echoed in validation errors. Preview payloads are signed, not encrypted.
- Localhost and `.localhost`, `.local`, `.internal` hostnames, non-public literal IP addresses, traversal paths, login/search/cart/checkout/calendar paths, non-content assets and known filter/session URL traps are rejected using existing metadata safety checks.
- Hostnames are **not resolved** during upload. Import success does not prove a hostname exists, is publicly reachable, permits collection or will yield contacts. DNS/public-address and robots checks occur later through the authorized collector.
- No scraped person/email/contact data is asserted by importing a source.

### Path scope

Paths are **plain decoded prefixes on the starting URL's exact origin**, not complete URLs, wildcards or regexes.

- Use `["/team/", "/contact/"]`, not `"/team/"` and not a newline-delimited string.
- Every prefix must begin with `/`. Percent signs, whitespace, control characters, backslashes, `..`, doubled slashes (`//`), standalone dot segments (`/./`), query strings and fragments are rejected.
- `/team/` covers `/team` and descendants such as `/team/sales/`, but not `/teamwork`.
- `/` covers the whole origin; use it only when you intend to review that broader scope. It does not cover another hostname, a `www` variant, or another protocol.
- A default from `/team/alex` covers that page and its descendants, not sibling profiles. Supply a reviewed broader prefix such as `/team/` if that is your intention.
- If `follow_links` is `false`, recording broader prefixes does not itself turn link following on.

## Always-safe initial state

The importer explicitly fixes these settings for every **new** source:

```text
active = false
approved = false
approval_kind = operator
approval_notes = empty
setup_mode = rules_only
collector = http
extractor = rules
require_sales_role = true
recipe = empty
allow_homepage = false
```

Do **not** include these keys in JSON. They are rejected rather than trusted or silently ignored. Browser collection, custom extraction recipes, AI extraction, automated onboarding and approval notes must be configured/reviewed separately in the existing source controls. Starting pages alone do not guarantee the default extraction recipe will match the website.

## Duplicates, errors and repeat uploads

- Duplicate detection uses the stored normalized starting-page URL, not source name or company name.
- The first valid occurrence of a new URL is eligible for creation. Later occurrences in the same file are skipped, even when their names or settings differ.
- Existing URLs are skipped **without changing any existing settings**, review notes, approvals, active/paused state or automated setup. To change a source, use its Edit screen.
- Preview and confirmation both check for duplicates. If another operator imports the same URL between them, confirmation skips it and reports the actual result.
- If any row is invalid, **no sources from that file are created**. Correct the indicated row/field, then upload again. Validation covers duplicate rows too; a bad duplicate is not a way to hide invalid data.
- Confirmation is transactional: an unexpected database failure rolls back that batch rather than leaving a partial import.
- A discarded preview leaves no Source records. The app does not save the original upload as a retained file.
- The uploader is staff-only and confirmation requires CSRF protection. It does not add a public upload API.

### Troubleshooting

| Symptom | What to do |
|---|---|
| Malformed JSON / duplicate key | Check commas and double quotes; keep only one instance of each key. |
| Unsupported field | Remove fields not listed above, including `approved`, `active`, `notes`, `recipe`, `campaign_id`, and `$schema`. |
| Row URL rejected | Supply a public content URL without credentials or unsafe/special URL patterns. Do not use search results or authenticated pages. |
| Starting URL outside scope | Add the appropriate decoded path prefix to that row's `allowed_paths`. |
| Too large / too many sources | Split into files of at most 100 rows and 256 KiB each. |
| Preview expired / invalid | Upload the original file again using the same account that will confirm it. |
| Created source does not crawl | Expected: it needs manual review/approval and a separate start action. |
| Already exists | No update is performed. Open the existing source to change its settings. |

## Machine-readable schema and downloadable files

- `examples/bulk-sources.json` — complete example you can edit.
- `examples/bulk-sources.schema.json` — JSON Schema for editors and structural validation.
- `docs/BULK_SOURCE_IMPORT.md` — this operator guide.

All are downloadable from **Sources → Import JSON**. The schema checks structure, field names, types and basic bounds; application validation remains authoritative for strict integer syntax, URL safety, decoded scope, duplicate JSON keys and upload byte size. Do not embed a `$schema` field in the upload itself; configure your editor to use the separate schema.
