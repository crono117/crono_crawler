#!/usr/bin/env python3
"""Start the local webapp and one collector, then stop both on Ctrl+C."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        raise SystemExit("Run python3 setup.py first.")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    commands = [[str(python), "-u", "manage.py", "runserver", "127.0.0.1:8000", "--noreload"],
                [str(python), "-u", "manage.py", "worker"]]
    processes = []
    try:
        for cmd in commands:
            processes.append(subprocess.Popen(cmd))
        print("\nClearPay Lead Console: http://127.0.0.1:8000\nCtrl+C stops the webapp and worker.\n", flush=True)
        while not stop.wait(1):
            if any(p.poll() is not None for p in processes):
                print("A process exited; stopping the other process.", file=sys.stderr)
                break
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    return 0 if stop.is_set() else 1

if __name__ == "__main__":
    raise SystemExit(main())
