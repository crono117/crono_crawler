"""A bounded, local activity feed of what the worker is doing, for the Lab console view.

The worker appends one JSON line per step (lane switch, request, result) to a file in DATA_DIR;
the web process reads it and adds only operator review decisions (approve/dismiss). Lines carry
URLs, statuses, timings and job messages (counts), never names, emails or phone numbers. Writing is best-effort: it can never fail worker work.
"""
from contextlib import contextmanager
import contextvars
import json
import os
from pathlib import Path
import threading
import time

from django.conf import settings

LANES = ("discovery", "approval", "automation", "collector", "jev")
MAX_BYTES = 1_000_000
_lane = contextvars.ContextVar("activity_lane", default="worker")
_lock = threading.Lock()


def path():
    value = getattr(settings, "ACTIVITY_LOG", "")
    return Path(value) if value else None


@contextmanager
def lane(name):
    token = _lane.set(name)
    try:
        yield
    finally:
        _lane.reset(token)


def emit(kind, text, **fields):
    target = path()
    if target is None:
        return
    entry = {"t": round(time.time(), 3), "lane": fields.pop("lane", None) or _lane.get(), "kind": kind,
             "text": str(text)[:300], **{k: v for k, v in fields.items() if v not in (None, "")}}
    try:
        with _lock:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
            if target.stat().st_size > MAX_BYTES:
                _truncate(target)
    except OSError:
        pass


def _truncate(target):
    lines = target.read_bytes().splitlines(keepends=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(b"".join(lines[len(lines) // 2:]))
    os.replace(temporary, target)


def read(after=0.0, limit=400):
    """Entries newer than ``after`` (a timestamp), oldest first, at most ``limit``."""
    target = path()
    if target is None or not target.exists():
        return []
    try:
        size = target.stat().st_size
        with open(target, "rb") as handle:
            handle.seek(max(0, size - 250_000))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries = []
    for line in tail:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # first line of a mid-file seek, or a torn write
        if isinstance(entry, dict) and entry.get("t", 0) > after:
            entries.append(entry)
    return entries[-limit:]
