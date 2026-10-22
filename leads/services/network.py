"""Public HTTP fetches with DNS pinning, bounded bodies, and checked redirects."""
import http.client
import time
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
import re

MAX_BYTES = 6 * 1024 * 1024

class FetchError(Exception):
    def __init__(self, message, retryable=False, status=None, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after

def canonical_url(url):
    p = urlsplit(url.strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError("Use a public http:// or https:// URL without credentials.")
    host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if not host or any(c.isspace() for c in host):
        raise ValueError("Invalid hostname.")
    port = p.port
    if port not in (None, 80, 443) or (p.scheme == "https" and port == 80) or (p.scheme == "http" and port == 443):
        raise ValueError("Only standard HTTP and HTTPS ports are supported.")
    if "\\" in url or any(ord(c) < 32 for c in url):
        raise ValueError("Invalid URL characters.")
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit((p.scheme.lower(), netloc, p.path or "/", p.query, ""))

def origin(url):
    p = urlsplit(canonical_url(url))
    return f"{p.scheme}://{p.netloc}"

def same_site(approved_origin, target_origin):
    """Whether ``target_origin`` is the approved website itself: identical, an http->https upgrade,
    and/or the same host with or without one leading "www.". Sites routinely redirect between these
    canonical forms. A downgrade to http, any other subdomain, or another port is not the same site."""
    a, b = urlsplit(approved_origin), urlsplit(target_origin)
    if a.scheme != b.scheme and not (a.scheme == "http" and b.scheme == "https"):
        return False
    bare = lambda host: (host or "")[4:] if (host or "").startswith("www.") else (host or "")
    return a.port == b.port and bare(a.hostname) == bare(b.hostname) and "." in bare(a.hostname)

def in_scope(source, url):
    try:
        target = canonical_url(url)
        # An operator approved the website, so its canonical www/https redirect stays in scope.
        # Policy-created sources keep their exact origin.
        exact = origin(target) == origin(source.url)
        if not exact and not (getattr(source, "approval_kind", "operator") == "operator"
                              and same_site(origin(source.url), origin(target))):
            return False
        path = unquote(urlsplit(target).path)
        # Reject traversal segments so a prefix cannot hide a different destination.
        if ".." in path.split("/") or "\\" in path:
            return False
        if path == "/" and not urlsplit(target).query and getattr(source, "allow_homepage", False):
            return True
        return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
                   for prefix in source.allowed_paths.splitlines() if prefix.strip())
    except (ValueError, UnicodeError):
        return False

def public_addresses(host, port):
    try:
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except socket.gaierror as exc:
        raise FetchError("DNS lookup failed.", retryable=True) from exc
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise FetchError("Destination is not a public Internet address.")
    return sorted(addresses, key=lambda ip: ":" in ip)

class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self.address = address
    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)

class PinnedHTTPS(PinnedHTTP):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)

@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes
    redirect_chain: list = field(default_factory=list)
    @property
    def text(self):
        content_type = (self.headers.get("content-type", "") or "")
        # Header-declared charset always wins.
        if "charset=" in content_type.lower():
            # Be tolerant of spacing/quotes and parameter order.
            m = re.search(r"charset\s*=\s*([^;]+)", content_type, re.IGNORECASE)
            header_charset = (m.group(1) if m else "").strip(" \"'")
            try:
                return self.body.decode(header_charset or "utf-8", errors="replace")
            except LookupError:
                return self.body.decode("utf-8", errors="replace")
        # No charset in the header: sniff the first 4096 bytes for an HTML meta declaration.
        sniff = self.body[:4096]
        # Find each <meta ...> tag and search for a charset=... attribute (quoted or unquoted),
        # regardless of attribute order or presence of http-equiv/content.
        for tag_match in re.finditer(br"<meta\b[^>]*>", sniff, re.IGNORECASE | re.DOTALL):
            tag = tag_match.group(0)
            # Prefer quoted value if present.
            m = re.search(br"charset\s*=\s*([\"'])([^\"']+)\1", tag, re.IGNORECASE | re.DOTALL)
            candidate_bytes = None
            if m:
                candidate_bytes = m.group(2)
            else:
                m2 = re.search(br"charset\s*=\s*([a-z0-9._:-]+)", tag, re.IGNORECASE)
                if m2:
                    candidate_bytes = m2.group(1)
            if candidate_bytes:
                try:
                    candidate = candidate_bytes.decode("ascii", "ignore")
                    return self.body.decode(candidate, errors="replace")
                except LookupError:
                    # Unknown label; continue scanning or fall back below.
                    continue
        # Fallback: UTF-8 with replacement to avoid crashes.
        return self.body.decode("utf-8", errors="replace")

def fetch(url, user_agent, guard=None, max_bytes=MAX_BYTES, timeout=25, *, request_headers=None, allow_redirects=True, before_attempt=None):
    """Resolve once per hop; connect to that exact public IP with hostname TLS."""
    from .activity import emit
    started = time.monotonic()
    try:
        response = _fetch(url, user_agent, guard, max_bytes, timeout, request_headers=request_headers,
                          allow_redirects=allow_redirects, before_attempt=before_attempt)
    except FetchError as exc:
        emit("error", f"GET {url} -> {exc}", url=url, ms=round((time.monotonic() - started) * 1000))
        raise
    emit("fetch", f"GET {response.url} -> {response.status} ({len(response.body):,} bytes)", url=response.url,
         status=response.status, bytes=len(response.body), ms=round((time.monotonic() - started) * 1000),
         redirects=len(response.redirect_chain) or None)
    return response

def _fetch(url, user_agent, guard, max_bytes, timeout, *, request_headers, allow_redirects, before_attempt):
    redirects = []
    for _ in range(6):
        try:
            url = canonical_url(url)
        except (ValueError, UnicodeError) as exc:
            raise FetchError(str(exc)) from exc
        if guard and not guard(url):
            raise FetchError(f"URL or redirect falls outside the approved scope: {url[:200]}")
        if before_attempt:
            before_attempt(url)
        p = urlsplit(url)
        port = 443 if p.scheme == "https" else 80
        addresses = public_addresses(p.hostname, port)
        connection = None
        try:
            klass = PinnedHTTPS if p.scheme == "https" else PinnedHTTP
            connection = klass(p.hostname, addresses[0], port, timeout)
            connection.request("GET", urlunsplit(("", "", p.path, p.query, "")), headers={
                "User-Agent": user_agent, "Accept-Encoding": "identity", "Accept": "*/*", "Connection": "close",
                **(request_headers or {}),
            })
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in (301, 302, 303, 307, 308):
                if not allow_redirects:
                    raise FetchError("Redirects are disabled for this API request.")
                if not headers.get("location"):
                    raise FetchError("Redirect has no destination.")
                redirects.append({"url": url, "status": response.status})
                url = urljoin(url, headers["location"])
                continue
            if headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise FetchError("Server sent compressed content despite an identity request.")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError("Response exceeds the configured size limit.")
            return Response(url, response.status, headers, body, redirects)
        except FetchError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(f"Network request failed: {type(exc).__name__}.", retryable=True) from exc
        finally:
            if connection:
                connection.close()
    raise FetchError("Too many redirects.")

def require_success(response):
    if response.status == 429 or response.status >= 500:
        raise FetchError(f"HTTP {response.status}; retry scheduled.", retryable=True,
                         status=response.status, retry_after=response.headers.get("retry-after"))
    if response.status in (401, 403):
        raise FetchError(f"HTTP {response.status}; source needs review.", status=response.status)
    if not 200 <= response.status < 300:
        raise FetchError(f"HTTP {response.status}.", status=response.status)

def fetch_browser(url, user_agent, guard):
    """Render without stealth. Route every request through the public-only client."""
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FetchError("Browser support is not installed. See README browser setup.") from exc

    async def render():
        initial = await asyncio.to_thread(fetch, url, user_agent, guard)
        require_success(initial)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=user_agent, service_workers="block",
                                                    accept_downloads=False, ignore_https_errors=False)
                page = await context.new_page()
                requests = 0
                errors = []
                gate = asyncio.Semaphore(4)
                main_url = initial.url
                async def route_handler(route):
                    nonlocal requests, main_url
                    req = route.request
                    requests += 1
                    main = req.is_navigation_request() and req.frame == page.main_frame
                    if requests > 100 or req.method not in ("GET", "HEAD") or req.resource_type in ("image", "media", "font"):
                        await route.abort()
                        return
                    try:
                        async with gate:
                            # The initial fetch also resolves any redirect before page.goto.
                            res = initial if main and req.url == initial.url else await asyncio.to_thread(
                                fetch, req.url, user_agent, guard if req.is_navigation_request() else None,
                                MAX_BYTES, 15)
                        if main:
                            require_success(res)
                            main_url = res.url
                        headers = {k: v for k, v in res.headers.items() if k in (
                            "content-type", "access-control-allow-origin", "content-security-policy", "x-content-type-options")}
                        await route.fulfill(status=res.status, headers=headers, body=res.body)
                    except FetchError as exc:
                        if main:
                            errors.append(exc)
                        await route.abort()
                await context.route("**/*", route_handler)
                await context.route_web_socket("**/*", lambda ws: ws.close())
                await page.goto(initial.url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1500)
                if errors:
                    raise errors[0]
                html = await page.content()
                body = html.encode("utf-8")
                if len(body) > MAX_BYTES:
                    raise FetchError("Rendered page exceeds the size limit.")
                return Response(main_url, 200, {"content-type": "text/html; charset=utf-8"}, body)
            finally:
                await browser.close()
    try:
        return asyncio.run(asyncio.wait_for(render(), timeout=100))
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"Browser failed ({type(exc).__name__}). Check Chromium installation and run again.", retryable=True) from exc
