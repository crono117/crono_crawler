#!/usr/bin/env python3
"""Interactive local bootstrap. This is not a Python package setup script."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import venv
import shutil

def main():
    parser = argparse.ArgumentParser(description="Set up the local Lead Console.")
    parser.add_argument("--browser", action="store_true", help="Install optional Playwright and Chromium.")
    parser.add_argument("--demo", action="store_true", help="Queue the fictional offline demo.")
    parser.add_argument("--no-user", action="store_true", help="Skip interactive administrator creation.")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Use Python 3.11 or newer; Python 3.12 is recommended.")
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if not (root / ".venv").exists():
        # Ensure ensurepip is available; install the system venv package if missing.
        try:
            import ensurepip  # noqa: F401
        except Exception:
            apt = shutil.which("apt-get") or shutil.which("apt")
            sudo = shutil.which("sudo")
            # Try to install the versioned python-venv first, then fall back to python3-venv.
            pkg_versioned = f"python{sys.version_info.major}.{sys.version_info.minor}-venv"
            install_cmds = []
            if apt:
                if sudo:
                    install_cmds.append([sudo, apt, "update", "-y"])
                    install_cmds.append([sudo, apt, "install", "-y", pkg_versioned])
                    install_cmds.append([sudo, apt, "install", "-y", "python3-venv"])
                else:
                    install_cmds.append([apt, "update", "-y"])
                    install_cmds.append([apt, "install", "-y", pkg_versioned])
                    install_cmds.append([apt, "install", "-y", "python3-venv"])
                for cmd in install_cmds:
                    try:
                        subprocess.run(cmd, check=True)
                    except Exception:
                        # Continue to next attempt; we'll re-check ensurepip below.
                        pass
            # Re-check after attempted installation.
            try:
                import ensurepip  # noqa: F401
            except Exception:
                raise SystemExit(
                    "The virtual environment could not be created because ensurepip is unavailable. "
                    "Install the appropriate python*-venv package for your system and rerun."
                )
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
    print("\nReady. Run: python3 run-local.py\nThen open: http://127.0.0.1:8017")

if __name__ == "__main__":
    main()
