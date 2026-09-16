# ClearPay Lead Engine

Repository: [crono117/crono_crawler](https://github.com/crono117/crono_crawler). The active application lives on `main`. The repository's previous state is preserved on `archive/legacy-scraper-2026-09-16`; that snapshot contained only the Apache 2.0 license. Work on this application from `main`.

**Discovery trial:** this branch adds scheduled discovery campaigns, ranked exact URLs, sitemap exploration, source review and optional web search. See [the discovery setup and upgrade guide](docs/DISCOVERY.md). The same worker runs both collection and discovery; existing accounts and lead data are preserved.

**Zero-contact pilot follow-up:** dashboard recipe-review warnings, precise path/domain filters and an offline `inspect_recipe` command help diagnose successful crawls that yield no contacts. See [the Host Merchant / Hermes handoff](docs/HOST_MERCHANT_PILOT.md) for the controlled update and test sequence.

A self-hosted Python application that continuously collects, reviews and refreshes published professional contacts from sources you select. The initial focus is merchant-services sales representatives, with separate tags for POS, payroll, business funding, telecom, IT and commercial insurance.

**Local pilot:** Django + SQLite + a persistent background worker. Optional Playwright handles JavaScript pages. Optional Ollama extracts less structured pages with a model running on your own machine. There is no OpenAI integration or paid AI API requirement.

## Start on your computer

Install Python 3.12 and Git, clone this repository, and enter its directory. On Ubuntu/Pop!_OS, install `python3-venv` if creating a virtual environment reports that it is missing.

```bash
git clone https://github.com/crono117/crono_crawler.git
cd crono_crawler
python3 setup.py --demo
python3 run-local.py
```

The setup creates `.venv`, installs dependencies, generates a private `.env`, migrates the local database, and prompts you to create an administrator. It also queues an explicitly fictional demo source when `--demo` is supplied.

Open **http://127.0.0.1:8000** and sign in. The demo produces three fictional contacts, letting you inspect the complete collection/review workflow without fetching a real website. Omit `--demo` for an empty database.

On Windows, use `py setup.py --demo` and `py run-local.py`. The launch scripts handle the Windows virtualenv path; Windows has not been tested in this build. Linux is the verified platform.

Keep the terminal open and the computer awake while collecting. The server deployment is what makes this independent of your laptop. Restarting the app retains sources, contacts, schedules, and queued work. After an abrupt worker crash, its lease can take up to ten minutes to expire before a replacement resumes.

If you have already created an administrator, rerun setup with `--no-user` to avoid creating another.

## Your first real source

1. Open **Sources → Add a source** and enter the exact public team/directory URL. Start with a specific page, not a search engine or social-network profile search.
2. Set the source business type. This identifies where a contact was found; it does not claim that every person sells everything the business offers.
3. Review the source's collection terms and restrictions, record a short note, and mark it reviewed. Public display alone is not a blanket permission for every reuse.
4. Choose HTML first, a small page limit, and an appropriate delay/schedule. Review the robots policy and path scope. `https://www.example.com` and `https://example.com` are distinct origins: use the final canonical origin.
5. Supply a CSS recipe if the default common team-card selectors do not match the page. The example in `examples/css-recipe.json` shows the supported fields. A zero-contact run can mean that the page has no suitable named contacts or needs a different recipe.
6. Save, then choose **Start / resume**. Inspect **Collection runs** and review each resulting lead's evidence.

The regular collector follows in-scope links up to the run's page/depth limits and can save exact external URLs under **Discovery → Links from regular collection**. For ongoing exploration, create a **Discovery campaign** using approved starting sources. Campaigns rank links, read in-scope sitemaps, save new domains for review, and collect contacts from eligible pages. Optional Brave web search can find additional domains; it is disabled by default. See [Discovery](docs/DISCOVERY.md) for controls, limits and the offline demo.

Collection is scheduled while sources are active. **Pause** disables recurring collection and stops additional pages; an in-flight page may finish saving. Editing a source cancels its unfinished run and leaves it paused so the new configuration starts consistently.

## What is stored

| Item | Purpose |
| --- | --- |
| Person, company, title, published email/phone | Contact record; unconfirmed details stay blank |
| Source business type | Operator-assigned classification of the website/directory |
| Person service tags | Keyword matches in the saved person evidence |
| Page service tags | Broader context, not automatically attributed to the person |
| URL, evidence passage, extraction method, content hash, timestamps | Provenance and freshness review |
| Review state and notes | New, reviewed, rejected, or Do not contact |

The pilot uses conservative matching: name + non-generic email, otherwise name + company + source/page. It never merges people solely because they share an office phone or generic inbox. Some duplicates are preferable to merging different people; changed identities across sources can still create separate records. Do not contact applies to the stored identity, not a universal person-resolution service.

Published contact details are not mailbox-verified. Person-level ownership remains unconfirmed unless the evidence supports a human review; common/shared contacts are marked accordingly. A sales-related role/description is required by default. Disable that filter only for sources already curated to relevant representatives.

An unchanged page refreshes its timestamps without rerunning extraction. A changed, successfully extracted page marks missing observations as no longer seen. Fetch/extraction failures preserve prior evidence. The contact summary may retain older non-empty fields; the evidence panel shows the exact values seen in each observation.

CSV exports respect filters and exclude rejected/suppressed records. The app does not send emails, texts, or calls.

## Optional browser rendering

```bash
python3 setup.py --browser --no-user
```

If Chromium reports missing Linux libraries, install its system dependencies using the Playwright CLI in your virtual environment:

```bash
.venv/bin/python -m playwright install-deps chromium
```

Then choose **Browser** on the source. Rendering uses ordinary Chromium, not a stealth browser. It blocks writes, downloads, WebSockets, heavy media, and service workers; HTTP requests use the same public-only transport. Some dynamic sites that need cookies, POST-based APIs, authentication or third-party redirects will not work in this deliberately limited pilot. Start with HTML collection wherever possible.

Browser navigation and rendering do not remove IP blocks or grant access. A blocked or challenged source is paused for review instead of cycling proxies.

## Optional local AI

Install [Ollama](https://docs.ollama.com/quickstart) separately and download a model that fits your computer. Configure `.env` with the exact installed model name:

```dotenv
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=your-installed-model-name
```

Restart the app, then select **Local Ollama** for a source. The app sends extracted page text to that configured endpoint, requests structured JSON, and verifies each returned contact against a copied source passage. No tools are exposed to the model. Pages over 50,000 text characters require a CSS recipe or narrower source.

A configured endpoint is trusted administrator configuration. Keep it on your own machine/network if you want model processing to remain local. Model quality and memory use vary; the webapp/browser server budget does not automatically include enough RAM for local model inference. The default HTML workflow does not require a model.

## GitHub and Cursor cloud agents

The repository includes `AGENTS.md`, `.cursor/environment.json`, and a GitHub Actions workflow. The Cursor configuration installs the app, applies migrations, and starts the webapp and worker. It contains no credentials or real contact data.

After this code is in GitHub, connect the repository in Cursor and run its environment build. If Cursor requests an install command, use `python3 setup.py --no-user`. Agents can run the test suite immediately; create a temporary administrator separately if they need to interact with the browser UI. [Cursor's environment setup guide](https://cursor.com/docs/cloud-agent/setup) describes the account/repository connection and build process.

A useful first agent task is: "Read AGENTS.md, run the tests, then validate browser collection using synthetic fixtures. Keep the public-only networking and source approval controls intact."

## Development commands

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py test
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/smoke_local.py
```

The smoke test starts real web and worker processes on a temporary local port/database, exercises sign-in, review, export, pause/run and restart recovery, and removes its test data afterward. It does not change your working database or require third-party network access.

Separate terminals are also supported:

```bash
.venv/bin/python manage.py runserver 127.0.0.1:8000 --noreload
.venv/bin/python manage.py worker
```

`worker --once` performs one queue tick and exits; a tick may check robots or defer a throttled page, so it does not necessarily finish a run.

## Moving to a server

See [deployment and migration](docs/DEPLOYMENT.md). Docker Compose supplies PostgreSQL, a Gunicorn web process, static asset serving, and one persistent worker. It binds the web port to the server's loopback interface by default. Use an SSH tunnel for the private pilot, then configure HTTPS and access controls before broader team access.

## Validation and pilot limits

See [validation notes](docs/VALIDATION.md) for the exact checks performed and optional paths that still need real-environment testing. This is a functional local pilot, not a guarantee of compatibility with every website. It needs representative, approved sources to measure lead quality and collection throughput. It does not crawl behind authentication, bypass blocks, solve CAPTCHAs, or run rotating proxies.

The current worker is intentionally single-process. Browser fetches, AI extraction and the webapp can run on one machine, but heavy model inference should be planned separately. Authentication is for trusted staff; the pilot does not yet implement distinct salesperson/admin roles, SSO, or a comprehensive user-action audit trail.
