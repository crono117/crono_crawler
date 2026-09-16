"""Bounded sitemap parsing and an optional fixed-origin search adapter."""
import gzip
import io
import json
from urllib.parse import urlencode
from defusedxml.ElementTree import fromstring
from django.conf import settings
from leads.services.network import FetchError, fetch, require_success

MAX_SITEMAP_BYTES = 2 * 1024 * 1024
BRAVE_ORIGIN = "https://api.search.brave.com"
BRAVE_ENDPOINT = BRAVE_ORIGIN + "/res/v1/web/search"


def sitemap_entries(body):
    if body[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(MAX_SITEMAP_BYTES + 1)
    if len(body) > MAX_SITEMAP_BYTES:
        raise ValueError("Sitemap exceeds the decompressed size limit.")
    root = fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    tag = root.tag.rsplit("}", 1)[-1]
    if tag not in ("urlset", "sitemapindex"):
        raise ValueError("Expected an XML sitemap or sitemap index.")
    kind = "sitemap" if tag == "sitemapindex" else "page"
    entries = []
    for child in list(root)[:2000]:
        for loc in child:
            if loc.tag.rsplit("}", 1)[-1] == "loc" and loc.text:
                entries.append((kind, loc.text.strip()))
                break
    return entries


def search_ready():
    return settings.BRAVE_SEARCH_ENABLED and bool(settings.BRAVE_SEARCH_API_KEY)


def brave_search(query):
    if not search_ready():
        raise ValueError("Brave search is disabled or its API key is missing.")
    if not query.strip() or len(query) > 600 or len(query.split()) > 75:
        raise ValueError("Search query exceeds the provider limits.")
    url = BRAVE_ENDPOINT + "?" + urlencode({"q": query, "count": 20, "result_filter": "web", "text_decorations": "false"})
    response = fetch(url, settings.BOT_USER_AGENT, guard=lambda u: u.startswith(BRAVE_ENDPOINT + "?"),
                     max_bytes=2 * 1024 * 1024, request_headers={"X-Subscription-Token": settings.BRAVE_SEARCH_API_KEY},
                     allow_redirects=False)
    require_success(response)
    try:
        results = json.loads(response.body).get("web", {}).get("results", [])
        if not isinstance(results, list):
            raise ValueError("Invalid result list")
    except (ValueError, AttributeError, TypeError) as exc:
        raise FetchError("Search provider returned an invalid JSON response.") from exc
    return [row for row in results[:20] if isinstance(row, dict) and isinstance(row.get("url"), str)]
