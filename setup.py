#!/usr/bin/env python3
"""Interactive local bootstrap. This is not a Python package setup script."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import venv

def main():
    parser = argparse.ArgumentParser(description="Set up the local ClearPay Lead Console.")
    parser.add_argument("--browser", action="store_true", help="Install optional Playwright and Chromium.")
    parser.add_argument("--demo", action="store_true", help="Queue the fictional offline demo.")
    parser.add_argument("--no-user", action="store_true", help="Skip interactive administrator creation.")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Use Python 3.11 or newer; Python 3.12 is recommended.")
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if not (root / ".venv").exists():
        venv.EnvBuilder(with_pip=True).create(root / ".venv")
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements-browser.txt" if args.browser else "requirements.txt"], check=True)
    if not (root / ".env").exists():
        env = (root / ".env.example").read_text().replace("replace-with-a-long-random-secret", secrets.token_urlsafe(48))
        env += "\nPOSTGRES_PASSWORD=" + secrets.token_urlsafe(32) + "\n"
        (root / ".env").write_text(env)
        if os.name != "nt":
            (root / ".env").chmod(0o600)
    subprocess.run([str(python), "manage.py", "migrate", "--noinput"], check=True)
    if args.browser:
        subprocess.run([str(python), "-m", "playwright", "install", "chromium"], check=True)
    if not args.no_user:
        print("\nCreate an administrator (existing administrators are preserved).")
        subprocess.run([str(python), "manage.py", "createsuperuser"], check=True)
    if args.demo:
        subprocess.run([str(python), "manage.py", "init_demo"], check=True)
    print("\nReady. Run: python3 run-local.py\nThen open: http://127.0.0.1:8000")

if __name__ == "__main__":
    main()
