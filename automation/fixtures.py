"""Explicit offline demo only; never route arbitrary URLs into a mock response."""
from pathlib import Path
from django.conf import settings
from leads.services.network import Response, origin

DEMO_ORIGIN = "https://automation.example.test"


def demo_response(source, url):
    if source.collector != "demo" or origin(source.url) != DEMO_ORIGIN or origin(url) != DEMO_ORIGIN:
        raise ValueError("This source is not the fixed offline automation fixture.")
    if url == DEMO_ORIGIN + "/team/":
        return Response(url, 200, {"content-type": "text/html; charset=utf-8"},
                        (Path(settings.BASE_DIR) / "examples" / "automation-team.html").read_bytes())
    return Response(url, 404, {"content-type": "text/plain"}, b"No page in this fixed fixture.")
