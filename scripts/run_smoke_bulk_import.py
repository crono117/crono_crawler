"""Isolated runner for scripts/smoke_bulk_import.py.

The bulk-import smoke refuses to run beside a real .env or data directory. This
runner copies the current checkout's tracked and new (non-ignored) files into a
temporary `bulk-import-*` directory, runs the smoke there with a minimal,
generated environment, and removes the copy afterwards. It never reads, moves or
overwrites this checkout's .env, database or data directory.

Usage: .venv/bin/python scripts/run_smoke_bulk_import.py [--keep]
"""
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
PASS_THROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT")
SKIP_PARTS = {".env", ".venv", "data", "__pycache__", ".git"}


def checkout_files():
    try:
        listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                cwd=ROOT, check=True, capture_output=True).stdout.decode().split("\0")
    except (OSError, subprocess.CalledProcessError):
        listed = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file()]
    for name in filter(None, listed):
        path = Path(name)
        if not SKIP_PARTS.intersection(path.parts) and (ROOT / path).is_file():
            yield path


def main():
    keep = "--keep" in sys.argv[1:]
    workdir = Path(tempfile.mkdtemp(prefix="bulk-import-"))
    checkout, data, tmp = workdir / "checkout", workdir / "data", workdir / "tmp"
    for path in checkout_files():
        target = checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
    data.mkdir()
    tmp.mkdir()
    env = {key: os.environ[key] for key in PASS_THROUGH if key in os.environ}
    env.update(DJANGO_SECRET_KEY=secrets.token_urlsafe(48), JEV_MODE="off", BRAVE_SEARCH_ENABLED="0",
               DATA_DIR=str(data), TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "scripts/smoke_bulk_import.py"], cwd=checkout, env=env)
    if result.returncode or keep:
        print(f"Isolated smoke files kept at {workdir}", file=sys.stderr)
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
