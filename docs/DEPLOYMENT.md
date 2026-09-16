# Server deployment and migration

The local pilot should be exercised with a few approved sources before moving to an always-on host. Docker files are provided, but were not executed in the initial build environment because it did not have Docker. Validate them on the target host before relying on scheduled collection.

## Start a fresh server deployment

Install Docker Engine with its Compose plugin on a Linux host. Clone the private GitHub repository there. Create `.env` from `.env.example`, supply a unique random `DJANGO_SECRET_KEY`, and add a unique URL-safe `POSTGRES_PASSWORD`. Keep both out of Git. A password generated with `secrets.token_urlsafe(32)` is suitable for the Compose URL interpolation.

Leave the default localhost allowed hosts and insecure-cookie setting while using an SSH tunnel. Compose overrides `DJANGO_DEBUG` to `0` for web and worker.

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
docker compose logs -f worker
```

The web container migrates the database and collects static files before starting Gunicorn. The worker waits for web readiness. PostgreSQL data persists in the named `postgres_data` volume. Do not use `docker compose down -v` unless you deliberately want to erase that database.

On your own computer, open a tunnel to the server:

```bash
ssh -L 8000:127.0.0.1:8000 your-user@your-server
```

Then visit http://127.0.0.1:8000 locally. The app port and PostgreSQL are not exposed on the server's public interfaces by the supplied Compose configuration.

For browser sources, add `INSTALL_BROWSER=1` to `.env` and rebuild. Chromium and its system libraries will then be installed into the image. Keep one worker. The basic server deployment does not provision an Ollama model; configure a trusted, reachable Ollama endpoint separately if needed. A loopback Ollama address inside a container refers to that container, not the host.

## Bring existing local leads across

Stop `run-local.py` first, so no new pages are being saved during export. Keep an independent backup of the local database and `.env`.

```bash
.venv/bin/python manage.py dumpdata leads --indent 2 --output data/lead-data.json
```

Transfer that file privately to the server; do not commit it. The following assumes it is at `data/lead-data.json` in the server checkout and the target is a fresh database:

```bash
docker compose stop worker
docker compose cp data/lead-data.json web:/app/data/lead-data.json
docker compose exec web python manage.py loaddata /app/data/lead-data.json
docker compose exec web python manage.py shell -c "from leads.models import WorkerLease; WorkerLease.objects.all().update(token='')"
docker compose start worker
```

This transfers sources, observations, lead review notes/statuses, candidates, and job history. It intentionally does not transfer administrator accounts; create the new server account with `createsuperuser`. Do not import into a database that already contains conflicting lead IDs. Verify record counts, a representative evidence record, and source states after import, then remove the temporary export from both machines.

## Backups

For SQLite, use Python's SQLite backup API instead of copying only the main file while WAL writes are active:

```bash
.venv/bin/python -c "import sqlite3; src=sqlite3.connect('data/leads.sqlite3'); dst=sqlite3.connect('data/leads-backup.sqlite3'); src.backup(dst); dst.close(); src.close()"
```

For PostgreSQL, make a backup to your server's private backup directory:

```bash
docker compose exec -T db pg_dump -U clearpay -d clearpay -Fc > clearpay-backup.dump
```

Backups contain contact data. Store them outside the repository with restricted access and test restores. The application does not yet schedule off-host backups for you.

## Wider team access

Before publishing a public URL, put the application behind an HTTPS reverse proxy, set the exact `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`, enable `DJANGO_SECURE_COOKIES=1`, and configure authentication protections appropriate for your team. Keep the database and collector management off the public network. Review proxy header handling and Django's deployment checks with the actual proxy configuration.

```bash
docker compose exec web python manage.py check --deploy
```

The supplied private pilot is not advertised as a publicly hardened multi-user service. Add distinct permissions, an operator audit trail, account protection and data retention controls before wider rollout. Every current staff operator can change sources, review contacts and export data.

## Updates

Back up first, review repository changes, pull the approved commit and rebuild. Keep only one collector for a given database. Schema migrations are applied when the web container starts. Check worker logs and a small run after updating.

```bash
docker compose up -d --build
docker compose logs --tail 100 web worker
```
