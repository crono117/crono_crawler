"""Short-lived HTML stays on this worker's disk, outside every HTTP response."""
import hashlib
import os
import re
from pathlib import Path
from django.conf import settings


MAX_BYTES = 2 * 1024 * 1024


def directory():
    path = Path(settings.DATA_DIR) / "automation-private"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_html(page, html):
    data = html.encode("utf-8")
    if len(data) > MAX_BYTES * 3:
        raise ValueError("Decoded HTML exceeds the private snapshot limit.")
    key = hashlib.sha256(f"{page.pk}:{page.generation}:".encode() + data).hexdigest()
    path = directory() / key
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return key


def read_html(page):
    if not re.fullmatch(r"[a-f0-9]{64}", page.private_key):
        raise ValueError("Private snapshot is unavailable; probe again.")
    from django.utils import timezone
    if not page.expires_at or page.expires_at <= timezone.now():
        raise ValueError("Private snapshot expired; probe again.")
    return (directory() / page.private_key).read_text(encoding="utf-8")


def purge_expired():
    from django.utils import timezone
    from .models import ProbePage
    for page in ProbePage.objects.filter(expires_at__lte=timezone.now()).exclude(private_key="")[:100]:
        if re.fullmatch(r"[a-f0-9]{64}", page.private_key):
            (directory() / page.private_key).unlink(missing_ok=True)
        page.private_key = ""
        page.save(update_fields=["private_key"])
    # A crash between writing a file and committing its DB row can leave an orphan.
    root = Path(settings.DATA_DIR) / "automation-private"
    if root.exists():
        cutoff = timezone.now().timestamp() - 24 * 3600
        for index, path in enumerate(root.iterdir()):
            if index >= 1000:
                break
            if re.fullmatch(r"[a-f0-9]{64}", path.name) and path.is_file() and path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
