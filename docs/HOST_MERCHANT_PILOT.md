# Host Merchant pilot follow-up

This is the Hermes handoff for the existing home-desktop installation. The application update is on `feat/discovery-v1`; it does not change your local campaign settings or start another worker.

## What the first test established

The operator reported 10 successfully processed pages, zero validated contacts, 47 approved same-origin URLs and three social URLs pending review. The worker stayed healthy, with no reported access or robots failure. Those are useful crawling results, but do not yet establish extraction coverage or contact yield.

Public-page text was reviewed on September 16, 2026:

- [Executive team](https://hostmerchantservices.com/executive-team/): named leaders and sales roles are present. The inspected text showed general company contact details separately from the biographies, without individual email/phone details in those biographies.
- [Independent sales](https://hostmerchantservices.com/independent-sales/): an agent-program description and application invitation. Treat it as a possible route to useful pages, not an established directory of named agents.
- [Partners](https://hostmerchantservices.com/partners/): referral-program information and company partnerships. External companies still need separate source review.
- [Contact](https://hostmerchantservices.com/contact/): general company contact channels and a submission form. Form fields are not published person records.

This is a source-suitability assessment, not a verified CSS recipe. Direct HTML fetching failed at DNS resolution in the development workspace. A separate cloud browser encountered the site's security verification, and inspection stopped. No production Host Merchant selectors were verified, no live contacts were harvested here, and no Host Merchant HTML or contact export is included in this repository. Hermes must inspect the normal, accessible page on the desktop before adding a recipe. Do not attach footer sales/support addresses or the switchboard to every named executive.

## Update safely

1. In the existing checkout, inspect `git status` and record the deployed commit. Preserve local changes. Stop web and worker through their existing service manager. Back up the actual database and `.env` privately as described in [the upgrade guide](DISCOVERY.md#upgrade-an-existing-local-installation).
2. Update the existing branch using a normal fast-forward. Do not reset local changes:

   ```bash
   git fetch origin
   git switch feat/discovery-v1
   git merge --ff-only origin/feat/discovery-v1
   python3 setup.py --no-user
   .venv/bin/python manage.py check
   .venv/bin/python manage.py test
   .venv/bin/python scripts/smoke_local.py
   ```

3. Before restarting the worker, pause the real campaign through the dashboard with only the web process running. Restart the normal launcher/service afterward with exactly one worker. Do not run the launcher alongside separately managed processes.
4. Existing completed zero-contact runs should immediately show **Recipe review needed** on the lead overview, Discovery dashboard, campaign page and run details. No rerun or data migration is needed to populate this warning. Per-page diagnostic counts become available on freshly extracted pages after this update.

## Filter the existing campaign first

Edit the Host Merchant campaign. Select **Add contact discovery exclusions**. It appends these rules while preserving existing exclusions:

```text
path:blog
path:article
path:emv-credit-card-machines
path:product
path:software
domain:facebook.com
domain:linkedin.com
domain:x.com
domain:twitter.com
```

`path:` rules match only the URL path, including simple plural forms and hyphen/space variants. A team page describing software products in its text can still qualify. `domain:` rules match the exact hostname and subdomains, never a different hostname that merely contains the string. Plain phrases still match path, hostname, label and local context. Remove `path:software` for a campaign that intentionally explores software reseller directories.

Saving leaves the campaign paused, cancels its unfinished run, and recalculates scores for all saved URLs, including the existing 47. Review **URLs → Approved scope** and the score explanations. Approval identifies permitted scope; it does not mean every approved URL is eligible to fetch. Negative scores are filtered out. Existing pending social URLs retain their review status and are not fetched.

After checking the exclusions, set **Minimum page priority** to **35**, **Maximum pages** to **5–10**, depth to **1–2**, and leave search off. Consider disabling sitemaps for this controlled extraction check; sitemap jobs share the page budget. Reviewed starting URLs are still visited unless filtered out. Team, sales, agent, partner, representative, executive and contact URLs receive priority; nearby team prose no longer gives product links the same bonus.

## Inspect and preview a source-specific recipe

Use the browser's normal accessible page/DOM to find the smallest repeatable person container. Check that each candidate card actually publishes a person's name, relevant role and associated email or phone. If the details are absent, record that result and keep zero valid contacts; a CSS selector cannot supply missing data.

Save HTML and candidate recipe files outside Git, for example in your private local `data/recipe-review/` directory. The crawler does not retain full raw HTML. Use source IDs from **Sources**; do not assume IDs from another installation. Start with `examples/css-recipe.json` as a syntax example, replacing selectors using the actual inspected HTML. It is not a Host Merchant recipe.

Supported keys:

| Key | Scope |
| --- | --- |
| `row` | One person/card container per match |
| `name`, `title`, `email`, `phone`, `company` | Relative to that card; mailto/tel targets are read as contact values, and visible text in a selected contact element is reduced to its address |
| `evidence` | Optional smaller container inside the card; blank uses the whole card |

The evidence container must contain the person's name and the accepted contact details. An unmatched evidence selector rejects the card; it never falls back to the whole page. When no selected email or phone is usable, a single visible address inside the evidence container is used only if the card names exactly one person and the address is not a generic inbox, a header/footer/navigation contact, or repeated in another card. Keep the sales-role requirement enabled. Company context supplied in source settings remains operator context.

Offline preview, replacing `SOURCE_ID` with the real numeric ID:

```bash
.venv/bin/python manage.py inspect_recipe --source SOURCE_ID --html data/recipe-review/page.html --recipe data/recipe-review/recipe.json
.venv/bin/python manage.py inspect_recipe --source SOURCE_ID --html data/recipe-review/page.html --recipe data/recipe-review/recipe.json --show-records
```

This command performs no HTTP requests, calls no model, and writes no contacts or source settings. It always tests CSS. The default report contains selectors and counts; `--show-records` also prints accepted fields and their evidence for local review. Do not commit saved pages or output containing real contacts. The preview reads UTF-8 HTML, replacing undecodable bytes; live fetching still uses the response's declared encoding.

Diagnostics distinguish no matching person cards, missing/invalid names, missing contact selectors, unsupported contacts, missing evidence containers, overly broad evidence and non-sales roles. Counts describe the first 500 matched cards, with a limit flag if more were present. A missing-contact result can mean a selector mismatch or that the page does not publish contact details; inspect the card to distinguish them.

Only save a source recipe after reviewing the actual evidence. Source edits leave regular collection paused and pause associated active campaigns. This update changes the extraction signature so unchanged pages are re-examined once using the new diagnostics and recipe behavior; subsequent unchanged refreshes retain the normal cache.

## Controlled collection and comparison

1. Verify that the source review covers the exact origin and selected starting paths. Review the three suggested Host Merchant pages individually before enabling them. New sources can have overlapping scope; separate campaigns prevent the first approved source's recipe from being used for a different candidate's test.
2. Run the Host Merchant campaign at the 5–10-page limit. Keep the source's separate recurring collection off to avoid duplicate work. Inspect each page's result and **Lead database → Source evidence** for every returned record.
3. Pause the campaign after completion while evaluating the result; Start/resume enables its recurring schedule. A completed run with no contacts remains a visible recipe-review warning, not a network failure. A refresh with existing validated contacts clears the current warning even if no new leads were created.
4. Record pages completed, cards matched/rejected, validated observations, new contacts, pending external URLs and whether each field is supported. Do not compare only the count of discovered URLs.
5. After documenting the Host Merchant result, test **Team Merchant — Our Team**, then **Florida Merchant Services — Our Team**, then the **Wells Fargo individual consultant page**. Verify the exact current URLs locally; no unverified candidate URL is supplied here. Use a separate reviewed source and campaign for each, one active test at a time, the same small limits and the same evidence checks. Stop/pause on access restrictions.

The worker's approved-origin, path, robots, request-delay, public-address, manual external-review and single-worker controls continue to apply. Discovery relevance never supplies missing contact evidence.
