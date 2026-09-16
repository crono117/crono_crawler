import socket
from unittest.mock import patch
from django.test import SimpleTestCase
from leads.models import Source
from leads.services.network import FetchError, canonical_url, fetch, in_scope, public_addresses

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
