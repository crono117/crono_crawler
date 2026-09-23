# Neutral console branding — branding-only release

The user requested removal of ClearPay product branding from the running scraper. This release changes only the neutral branding surfaces:

- Sidebar: **Lead Console**, with the old wordmark and C logo removed.
- Login page: **LEAD CONSOLE**.
- Console browser titles: **Lead Console** suffix.
- Django admin: **Lead Console administration** / **Lead Console admin**.
- CSV downloads: `leads.csv`; contents and suppression unchanged.

This release deliberately excludes the separately prepared Found on/source-link feature. Its base is deployed revision `cf1a8439cd36d7d4a6694112da1dfafe61a92780`. The reviewed branding change from `fb977e4c74306e052371c20456e68f45d4caaa9b` was isolated onto this base; the template merge retains the existing stylesheet list, without the source-link feature's stylesheet.

Company names, recorded URLs and collected evidence are not rewritten. Service names, bind variables, signing salts and crawler identity remain unchanged for compatibility. No crawling or paid-provider activation is authorized by this UI change.

## Verified candidate

- **260 tests passed** in the branding-only release, including the three RED → GREEN branding regressions.
- Real-process local web/worker smoke passed and cleaned up its processes/data.
- Django checks passed; migration drift: **No changes detected**.
- Tests used production Python 3.11.15/dependencies with sanitized environment, temporary databases and an inherited loopback-only network guard.
- Original branding diff passed independent read-only review with no blocking correctness or security issues; non-blocking test-scope/fallback-title suggestions remain documented in its review artifact.
- No dependency changes, schema changes, contact edits, source approvals or remote pushes.

Candidate: `/home/crono/dev/crono_crawler-branding-release`, branch `release/neutral-branding-only`.
Verification logs: `/home/crono/.hermes/profiles/lead-scrape/cache/branding-deploy/`.

## Authorized deployment

Stop the existing service; create and decrypt/check an encrypted recovery archive; fingerprint persistent data and private configuration. Install the exact packaged candidate via local Git fetch into a dedicated deployment branch, without changing the existing .env or service. No bootstrap/install/migration is needed for these template/header-only changes. Run Django checks, restart the existing service, verify actual login/admin responses and read-only authenticated rendering, compare persistent data/configuration, and confirm the existing private bind and one-worker contract. On failure after source switch, restore the prior Git branch and restart it; never overwrite the database with an older backup as an automatic rollback.
