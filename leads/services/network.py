"""Public HTTP fetches with DNS pinning, bounded bodies, and checked redirects."""
import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

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

def in_scope(source, url):
    try:
        target = canonical_url(url)
        if origin(target) != origin(source.url):
            return False
        path = unquote(urlsplit(target).path)
        # Reject traversal segments so a prefix cannot hide a different destination.
        if ".." in path.split("/") or "\\" in path:
            return False
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
    @property
    def text(self):
        content_type = self.headers.get("content-type", "")
        charset = content_type.split("charset=")[-1].split(";")[0].strip(" \"'") if "charset=" in content_type else "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

def fetch(url, user_agent, guard=None, max_bytes=MAX_BYTES, timeout=25):
    """Resolve once per hop; connect to that exact public IP with hostname TLS."""
    for _ in range(6):
        try:
            url = canonical_url(url)
        except (ValueError, UnicodeError) as exc:
            raise FetchError(str(exc)) from exc
        if guard and not guard(url):
            raise FetchError("URL or redirect falls outside the approved scope.")
        p = urlsplit(url)
        port = 443 if p.scheme == "https" else 80
        addresses = public_addresses(p.hostname, port)
        connection = None
        try:
            klass = PinnedHTTPS if p.scheme == "https" else PinnedHTTP
            connection = klass(p.hostname, addresses[0], port, timeout)
            connection.request("GET", urlunsplit(("", "", p.path, p.query, "")), headers={
                "User-Agent": user_agent, "Accept-Encoding": "identity", "Accept": "*/*", "Connection": "close",
            })
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in (301, 302, 303, 307, 308):
                if not headers.get("location"):
                    raise FetchError("Redirect has no destination.")
                url = urljoin(url, headers["location"])
                continue
            if headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise FetchError("Server sent compressed content despite an identity request.")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError("Response exceeds the configured size limit.")
            return Response(url, response.status, headers, body)
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
