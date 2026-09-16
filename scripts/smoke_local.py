#!/usr/bin/env python3
"""Exercise real web/worker processes against a temporary synthetic database.

Run with .venv/bin/python scripts/smoke_local.py. No real contacts, credentials,
database contents, or third-party network access are needed or modified.
"""
import csv
import io
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="clearpay-smoke-") as directory:
        env = dict(os.environ, DATA_DIR=directory, DATABASE_URL="", DJANGO_DEBUG="1",
                   DJANGO_SECRET_KEY=secrets.token_urlsafe(48), DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost",
                   DJANGO_SECURE_COOKIES="0", DJANGO_SETTINGS_MODULE="config.settings")
        os.environ.update(env)
        import django
        django.setup()
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from django.db import connections
        from leads.models import Lead, Run, Source, WorkerLease
        from leads.services.worker import enqueue
        from bs4 import BeautifulSoup
        import httpx

        call_command("migrate", interactive=False, verbosity=0)
        call_command("init_demo", stdout=io.StringIO())
        password = secrets.token_urlsafe(24)
        user = get_user_model().objects.create_user("smoke-operator", password=password, is_staff=True)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        processes = []
        output = tempfile.TemporaryFile(mode="w+")

        def start():
            commands = [
                [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
                [sys.executable, "manage.py", "worker"],
            ]
            for command in commands:
                processes.append(subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT))

        def stop():
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            processes.clear()

        def wait_for(predicate, description):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if predicate():
                    return
                if any(p.poll() is not None for p in processes):
                    raise AssertionError("An app process exited unexpectedly.")
                time.sleep(0.1)
            raise AssertionError(description)

        def csrf(response):
            return BeautifulSoup(response.text, "html.parser").select_one("input[name=csrfmiddlewaretoken]")["value"]

        try:
            with httpx.Client(base_url=base, follow_redirects=True, timeout=5, trust_env=False) as client:
                def ready():
                    try:
                        return client.get("/login/").status_code == 200
                    except httpx.TransportError:
                        return False

                start()
                wait_for(ready, "The web process did not start.")
                wait_for(lambda: Lead.objects.count() == 3, "The worker did not collect the offline demo.")
                login = client.get("/login/")
                result = client.post("/login/", data={"username": user.username, "password": password,
                                                       "csrfmiddlewaretoken": csrf(login)})
                assert result.status_code == 200 and "Lead overview" in result.text
                assert client.get("/static/console.css").status_code == 200
                result = client.get("/leads/", params={"service": "payroll"})
                assert "Jordan Sample" in result.text and "Alex Example" not in result.text
                print("PASS: actual web/worker startup, login, CSS, collection and service filtering.")

                source = Source.objects.get(collector="demo")
                lead = Lead.objects.get(name="Alex Example")
                detail = client.get(f"/leads/{lead.pk}/")
                assert "Source evidence" in detail.text and "alex@example.com" in detail.text
                client.post(f"/leads/{lead.pk}/", data={"status": "suppressed", "notes": "Synthetic smoke review",
                                                        "csrfmiddlewaretoken": csrf(detail)})
                records = list(csv.DictReader(io.StringIO(client.get("/leads/export/").text)))
                assert {r["name"] for r in records} == {"Jordan Sample", "Taylor Fixture"}
                detail = client.get(f"/sources/{source.pk}/")
                client.post(f"/sources/{source.pk}/action/", data={"action": "pause", "csrfmiddlewaretoken": csrf(detail)})
                source.refresh_from_db()
                assert not source.active
                before = Run.objects.count()
                detail = client.get(f"/sources/{source.pk}/")
                client.post(f"/sources/{source.pk}/action/", data={"action": "run", "csrfmiddlewaretoken": csrf(detail)})
                wait_for(lambda: Run.objects.count() > before and Run.objects.first().status == "completed",
                         "Run now did not reach the worker.")
                lead.refresh_from_db()
                assert lead.status == "suppressed" and Lead.objects.count() == 3
                print("PASS: published evidence, review persistence, CSV suppression, pause/run controls and deduplication.")

                stop()
                assert not WorkerLease.objects.get(key="collector").token
                pending = enqueue(source)
                start()
                wait_for(ready, "The web process did not restart.")
                wait_for(lambda: Run.objects.get(pk=pending.pk).status == "completed",
                         "The restarted worker did not resume the persisted job.")
                lead.refresh_from_db()
                assert lead.status == "suppressed" and Lead.objects.count() == 3
                assert "Lead overview" in client.get("/").text
                print("PASS: full process restart resumes a queued job and retains the login session and review decisions.")
        except Exception:
            stop()
            output.seek(0)
            print(output.read()[-6000:], file=sys.stderr)
            raise
        finally:
            stop()
            output.close()
            connections.close_all()
    print("PASS: test processes stopped and temporary database removed.")


if __name__ == "__main__":
    main()
