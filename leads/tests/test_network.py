import socket
from unittest.mock import patch
from django.test import SimpleTestCase
from leads.models import Source
from leads.services.network import FetchError, Response, canonical_url, fetch, in_scope, public_addresses, same_site

class NetworkTests(SimpleTestCase):
    def test_url_rejects_credentials_protocols_and_nonstandard_ports(self):
        for url in ("file:///etc/passwd", "http://user:pass@example.com", "http://example.com:8000", "https://example.com\\@localhost", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_url(url)

    def test_scope_requires_same_origin_and_path_boundary(self):
        source = Source(url="https://example.com/team/", allowed_paths="/team")
        self.assertTrue(in_scope(source, "https://example.com/team/alex"))
        self.assertFalse(in_scope(source, "https://example.com/teams"))
        self.assertFalse(in_scope(source, "https://other.example.com/team/"))
        self.assertFalse(in_scope(source, "https://example.com/team/%2e%2e/private"))

    def test_same_site_allows_only_www_and_https_canonical_forms(self):
        for approved, target in (("https://www.example.com", "https://example.com"),
                                 ("https://example.com", "https://www.example.com"),
                                 ("http://example.com", "https://www.example.com")):
            with self.subTest(target=target):
                self.assertTrue(same_site(approved, target))
        for approved, target in (("https://example.com", "http://example.com"),       # downgrade
                                 ("https://www.example.com", "https://shop.example.com"),
                                 ("https://www.example.com", "https://www.example.com.evil.test"),
                                 ("https://www.example.com", "https://wwwexample.com"),
                                 ("https://www.example.com", "https://www.www.example.com"),
                                 ("https://www.com", "https://com")):
            with self.subTest(target=target):
                self.assertFalse(same_site(approved, target))

    def test_operator_sources_keep_their_canonical_redirect_but_policy_sources_stay_exact(self):
        operator = Source(url="https://www.example.com/", allowed_paths="/team", approval_kind="operator")
        self.assertTrue(in_scope(operator, "https://example.com/team/alex"))
        self.assertFalse(in_scope(operator, "https://example.com/private"))  # path rules still apply
        self.assertFalse(in_scope(operator, "https://shop.example.com/team/"))
        policy = Source(url="https://www.example.com/", allowed_paths="/team", approval_kind="policy")
        self.assertFalse(in_scope(policy, "https://example.com/team/alex"))

    @patch("leads.services.network.socket.getaddrinfo")
    def test_private_and_mixed_dns_answers_are_rejected(self, dns):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "192.168.1.1"):
            dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]
            with self.subTest(ip=ip), self.assertRaises(FetchError):
                public_addresses("source.example.com", 80)
        dns.return_value += [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        with self.assertRaises(FetchError):
            public_addresses("source.example.com", 80)

    @patch("leads.services.network.PinnedHTTP")
    @patch("leads.services.network.socket.getaddrinfo")
    def test_redirect_cannot_reach_private_network(self, dns, connection):
        dns.side_effect = [[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))],
                           [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]]
        res = connection.return_value.getresponse.return_value
        res.status = 302
        res.getheaders.return_value = [("Location", "http://127.0.0.1/secret")]
        with self.assertRaises(FetchError):
            fetch("http://example.com/", "TestBot")
        self.assertEqual(connection.call_count, 1)

    @patch("leads.services.network.PinnedHTTP")
    @patch("leads.services.network.public_addresses", return_value=["8.8.8.8"])
    def test_body_size_is_bounded(self, dns, connection):
        res = connection.return_value.getresponse.return_value
        res.status = 200
        res.getheaders.return_value = [("Content-Type", "text/html")]
        res.read.return_value = b"123456"
        with self.assertRaises(FetchError):
            fetch("http://example.com/", "TestBot", max_bytes=5)
        connection.assert_called_once_with("example.com", "8.8.8.8", 80, 25)


class ResponseTextTests(SimpleTestCase):
    def test_meta_charset_windows_1252_decodes_correctly(self):
        # Body contains accented characters encoded as Windows-1252; no charset in header.
        body_text = "<!doctype html><html><head><meta charset=\"windows-1252\"></head><body>François & Mário</body></html>"
        body_bytes = body_text.encode("cp1252")
        res = Response(
            url="https://example.com/",
            status=200,
            headers={"content-type": "text/html"},
            body=body_bytes,
        )
        self.assertIn("François", res.text)
        self.assertIn("Mário", res.text)

    def test_header_charset_beats_meta(self):
        # Body is Windows-1252 but header declares UTF-8; header must win over meta.
        body_text = "<!doctype html><html><head><meta charset=\"windows-1252\"></head><body>Jérôme & Anaïs</body></html>"
        body_bytes = body_text.encode("cp1252")
        res = Response(
            url="https://example.com/",
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body_bytes,
        )
        self.assertEqual(res.text, body_bytes.decode("utf-8", errors="replace"))

    def test_unknown_meta_charset_falls_back_utf8(self):
        # Unknown meta charset should not crash and should fall back to UTF-8 default.
        body_text = "<html><head><meta charset=\"x-unknown-charset\"></head><body>Hello – world</body></html>"
        # Use valid UTF-8 bytes so fallback produces an intact string.
        body_bytes = body_text.encode("utf-8")
        res = Response(
            url="https://example.com/",
            status=200,
            headers={"content-type": "text/html"},
            body=body_bytes,
        )
        self.assertEqual(res.text, body_text)

    def test_meta_beyond_4096_is_ignored(self):
        # Place the meta tag after 4096 bytes; without a header charset, fallback is UTF-8 replace.
        prefix = ("<html><head>" + (" " * 4100)).encode("ascii")
        meta = '<meta charset="windows-1252">'.encode("ascii")
        content_cp1252 = "<body>François</body></html>".encode("cp1252")
        body_bytes = prefix + meta + content_cp1252
        res = Response(
            url="https://example.com/",
            status=200,
            headers={"content-type": "text/html"},
            body=body_bytes,
        )
        # If meta were honored, we'd see the proper ç; instead, it should not equal cp1252 decoding.
        self.assertNotEqual(res.text, body_bytes.decode("cp1252"))
