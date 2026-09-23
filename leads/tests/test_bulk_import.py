import importlib.util
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from leads.models import Source


def document(*rows):
    return {"schema_version": 1, "sources": list(rows) or [{"name": "Example team", "url": "https://example.com/team"}]}


class SourceImportServiceTests(TestCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("leads.services.source_import"), "Bulk source service must exist")
        from leads.services import source_import
        return source_import

    def test_preview_and_duplicate_import_never_update_existing_sources(self):
        service = self.service()
        existing = Source.objects.create(name="Keep", url="https://example.com/team", active=True, approved=True, approval_kind="policy", setup_mode="automatic", recipe={"row": ".keep"}, approval_notes="Keep decision", last_error="Paused previously")
        before = Source.objects.values().get(pk=existing.pk)
        rows = document({"name": "Overwrite?", "url": existing.url}, {"name": "New", "url": "https://example.org/team"}, {"name": "Repeated", "url": "https://EXAMPLE.org:443/team#staff"})
        preview = service.import_sources(rows)
        self.assertEqual(Source.objects.count(), 1)
        self.assertEqual([r["outcome"] for r in preview["results"]], ["existing", "would_create", "duplicate_in_file"])
        self.assertEqual((preview["would_create"], preview["skipped"]), (1, 2))
        applied = service.import_sources(rows, apply=True)
        self.assertEqual((applied["created"], applied["skipped"]), (1, 2))
        repeated = service.import_sources(rows, apply=True)
        self.assertEqual((repeated["created"], repeated["skipped"]), (0, 3))
        self.assertEqual(Source.objects.values().get(pk=existing.pk), before)
        self.assertEqual(Source.objects.count(), 2)

    def test_valid_full_settings_and_decoded_default_scope(self):
        service = self.service()
        row = {"name": " Team ", "url": "https://example.com/%C3%A9quipe/sales", "company": " Société ", "category": "pos", "allowed_paths": ["/équipe", "/directory"], "max_pages": 500, "max_depth": 5, "delay_seconds": 3600, "interval_hours": 8760, "follow_links": True, "discover_external": True}
        service.import_sources(document(row), apply=True)
        source = Source.objects.get()
        for key, expected in row.items():
            if key == "allowed_paths":
                expected = "/équipe\n/directory"
            elif key in ("name", "company"):
                expected = expected.strip()
            self.assertEqual(getattr(source, key), expected, key)
        service.import_sources(document({"name": "Other", "url": "https://example.org/%C3%A9quipe"}), apply=True)
        self.assertEqual(Source.objects.get(name="Other").allowed_paths, "/équipe")

    def test_all_rows_validate_before_any_write(self):
        service = self.service()
        rows = document({"name": "Valid", "url": "https://example.com/team"}, {"name": "Bad", "url": "https://example.org/team", "approved": True})
        with patch.object(Source.objects, "get_or_create", wraps=Source.objects.get_or_create) as create:
            with self.assertRaisesRegex(ValueError, "Row 2"):
                service.import_sources(rows, apply=True)
            create.assert_not_called()
        self.assertEqual(Source.objects.count(), 0)

    def test_database_failure_rolls_back_entire_batch(self):
        from django.db import DatabaseError
        service = self.service()
        create = Source.objects.get_or_create
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(kwargs["url"])
            if len(calls) == 2:
                raise DatabaseError("Synthetic database failure")
            return create(*args, **kwargs)

        with patch.object(Source.objects, "get_or_create", side_effect=fail_second):
            with self.assertRaises(DatabaseError):
                service.import_sources(document(
                    {"name": "First", "url": "https://example.com/team"},
                    {"name": "Second", "url": "https://example.org/team"},
                ), apply=True)
        self.assertEqual(len(calls), 2)
        self.assertFalse(Source.objects.exists())

    def test_competing_insert_is_skipped_without_overwrite(self):
        service = self.service()
        existing = Source.objects.create(name="Concurrent operator", url="https://example.com/team", approved=False, active=False)
        before = Source.objects.values().get(pk=existing.pk)
        with patch.object(Source.objects, "filter") as lookup:
            lookup.return_value.first.return_value = None
            result = service.import_sources(document(), apply=True)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(Source.objects.values().get(pk=existing.pk), before)

    def test_schema_types_required_fields_and_boundaries(self):
        service = self.service()
        invalid_documents = [None, [], {}, {"schema_version": True, "sources": document()["sources"]}, {"schema_version": 2, "sources": document()["sources"]}, {"schema_version": 1.0, "sources": document()["sources"]}, {"schema_version": 1, "sources": []}, {"schema_version": 1, "sources": document()["sources"] * 101}, {**document(), "extra": "secret"}]
        invalid_rows = [None, [], {}, {"name": "Only"}, {"url": "https://example.com"}]
        for field, values in {
            "name": [None, 1, True, "", " " * 4, "x" * 161, "line\nbreak", "zero\u200bwidth", "tab\t", "space\u00a0inside"],
            "company": [False, [], "x" * 161, "\rsecret"],
            "category": [None, "bogus", 1, []],
            "url": [None, 2, "x" * 1501],
            "allowed_paths": ["/", [], ["/"] * 11, [None], [""], ["x"], ["/a?b"], ["/a#b"], ["/a%20b"], ["/a\\b"], ["/a b"], ["/a\nb"], ["/a..b"], ["/a/./b"], ["//a"], ["/" + "x" * 1500], ["/other"]],
            "max_pages": [0, 501, True, 1.0, "5", None], "max_depth": [-1, 6, False],
            "delay_seconds": [1, 3601, float("inf")], "interval_hours": [0, 8761, float("nan")],
            "follow_links": [0, "false", None], "discover_external": [1, "true"],
        }.items():
            for value in values:
                invalid_rows.append({**document()["sources"][0], field: value})
        for field in ("active", "approved", "approval_notes", "recipe", "setup_mode", "collector", "extractor", "require_sales_role", "allow_homepage", "unknown", "unsafe-password-key"):
            invalid_rows.append({**document()["sources"][0], field: "secret"})
        for row in invalid_rows:
            invalid_documents.append({"schema_version": 1, "sources": [row]})
        for data in invalid_documents:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    service.import_sources(data, apply=True)
        self.assertEqual(Source.objects.count(), 0)

    def test_url_safety_errors_never_echo_credentials(self):
        service = self.service()
        urls = [" https://example.com", "https://example.com ", "https://example.com/a b", "https://user:SECRET@example.com/team", "ftp://example.com/team", "https://example.com:8443/team", "https://example.com:80/team", "http://example.com:443/team", "https://localhost/team", "https://127.0.0.1/team", "https://[::1]/team", "https://192.168.1.1/team", "https://example.local/team", "https://example.com\\@evil.com/team", "https://example.com/a%5Cb", "https://example.com/a%00b", "https://example.com/a%0db", "https://example.com/a\u200bb", "https://example.com/../team", "https://example.com/%2e%2e/team", "https://example.com/%252e%252e/team", "https://example.com//team", "https://example.com/./team", "https://example.com/a%3fb", "https://example.com/a%23b", "https://example.com/team.pdf"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as error:
                    service.import_sources(document({"name": "Valid", "url": url}), apply=True)
                self.assertIn("Row 1: url", str(error.exception))
                self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(Source.objects.count(), 0)

    def test_bounded_strict_utf8_json_parser(self):
        service = self.service()
        self.assertTrue(hasattr(service, "parse_upload"), "Strict file parser is required")
        raw = json.dumps(document(), ensure_ascii=False).encode("utf-8")
        self.assertEqual(service.parse_upload(raw), document())
        self.assertEqual(service.parse_upload(b"\xef\xbb\xbf" + raw), document())
        self.assertEqual(service.parse_upload(raw + b" " * (262144 - len(raw))), document())
        bad = [b"", b"\xff", b"\xff\xfe" + json.dumps(document()).encode("utf-16-le"), b"{", raw + b" " * 262144,
               b'{"schema_version":1,"schema_version":1,"sources":[]}',
               b'{"schema_version":1,"sources":[{"name":"One","name":"Two","url":"https://example.com/team"}]}',
               b'{"schema_version":1,"sources":[{"name":"One","url":"https://example.com/team","max_pages":NaN}]}',
               b'{"schema_version":1,"sources":[{"name":"One","url":"https://example.com/team","max_pages":Infinity}]}',
               b'{"schema_version":1,"sources":[{"name":"One","url":"https://example.com/team","max_pages":1e999}]}',
               b"[" * 2000 + b"0" + b"]" * 2000]
        for body in bad:
            with self.subTest(body=body[:100]):
                with self.assertRaises(ValueError):
                    service.parse_upload(body)
        rows = [{"name": str(i), "url": f"https://example.com/team/{i}", "allowed_paths": ["/team", "/" + "x" * 1499, "/" + "y" * 1499]} for i in range(100)]
        with self.assertRaisesRegex(ValueError, "256 KiB"):
            service.import_sources(document(*rows), apply=True)
        self.assertEqual(Source.objects.count(), 0)

    def test_minimal_import_is_explicitly_inert_and_preserves_query(self):
        service = self.service()
        data = document({"name": "  Équipe  ", "url": "https://EXAMPLE.com:443/team?z=2&utm_source=test&a=1#people"})
        with patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")), patch("leads.services.network.fetch", side_effect=AssertionError("No fetch")):
            result = service.import_sources(data, apply=True)
        source = Source.objects.get()
        self.assertEqual(result["created"], 1)
        self.assertEqual(source.name, "Équipe")
        self.assertEqual(source.url, "https://example.com/team?z=2&utm_source=test&a=1")
        for field, value in {"company": "", "category": "merchant_services", "allowed_paths": "/team", "active": False, "approved": False, "approval_kind": "operator", "approval_notes": "", "setup_mode": "rules_only", "collector": "http", "extractor": "rules", "require_sales_role": True, "recipe": {}, "allow_homepage": False, "max_pages": 5, "max_depth": 1, "delay_seconds": 5, "interval_hours": 168, "follow_links": False, "discover_external": False}.items():
            self.assertEqual(getattr(source, field), value, field)


class SourceImportBrowserTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(username="bulk-staff", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def preview(self, data=None, *, body=None, filename="sources.json", client=None):
        upload = SimpleUploadedFile(filename, body if body is not None else json.dumps(data or document()).encode("utf-8"), content_type="application/json")
        return (client or self.client).post("/sources/import/", {"action": "preview", "file": upload})

    def test_fixed_downloads_are_staff_only_and_example_really_validates(self):
        from leads.services.source_import import parse_upload
        from django.conf import settings
        from pathlib import Path

        for kind, relative, mime in (("example", "examples/bulk-sources.json", "application/json"), ("schema", "examples/bulk-sources.schema.json", "application/json"), ("guide", "docs/BULK_SOURCE_IMPORT.md", "text/markdown")):
            url = f"/sources/import/{kind}/"
            response = self.client.get(url + "?path=../../.env")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(reverse(f"source_import_{kind}"), url)
            self.assertTrue(response["Content-Type"].startswith(mime))
            self.assertIn("attachment", response["Content-Disposition"])
            self.assertIn(Path(relative).name, response["Content-Disposition"])
            content = b"".join(response.streaming_content) if response.streaming else response.content
            self.assertEqual(content, (settings.BASE_DIR / relative).read_bytes())
            if kind == "example":
                self.assertTrue(parse_upload(content)["sources"])
            if kind == "schema":
                self.assertEqual(json.loads(content)["type"], "object")
            self.assertEqual(Client().get(url).status_code, 302)
            nonstaff = Client()
            user, _ = get_user_model().objects.get_or_create(username="download-nonstaff")
            nonstaff.force_login(user)
            self.assertEqual(nonstaff.get(url).status_code, 302)
            self.assertEqual(self.client.post(url).status_code, 405)
            self.assertEqual(self.client.put(url).status_code, 405)
            self.assertContains(self.client.get("/sources/import/"), url)
        self.assertFalse(Source.objects.exists())

    def test_access_csrf_and_method_controls(self):
        url = "/sources/import/"
        for client in (Client(),):
            self.assertEqual(client.get(url).status_code, 302)
            self.assertEqual(self.preview(client=client).status_code, 302)
        for staff, active in ((False, True), (True, False)):
            user = get_user_model().objects.create_user(username=f"denied-{staff}", is_staff=staff, is_active=active)
            client = Client()
            client.force_login(user)
            self.assertEqual(client.get(url).status_code, 302)
            self.assertEqual(self.preview(client=client).status_code, 302)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.staff)
        self.assertEqual(self.preview(client=csrf).status_code, 403)
        self.assertEqual(csrf.post(url, {"action": "import", "preview_token": "anything"}).status_code, 403)
        self.assertEqual(self.client.put(url).status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(self.client.post(url, {"action": "unknown"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"sources": json.dumps(document())}).status_code, 400)

    def test_initial_import_page_shows_valid_example_before_upload(self):
        from bs4 import BeautifulSoup
        from leads.services.source_import import parse_upload
        response = self.client.get('/sources/import/')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        panel = soup.select_one('#json-format-example')
        self.assertIsNotNone(panel, 'Example must appear before any upload or error')
        self.assertTrue(parse_upload(panel.select_one('pre code').get_text().encode('utf-8'))['sources'])
        self.assertEqual(len(soup.select('#json-format-example')), 1)
        self.assertIsNone(soup.select_one('[role=alert]'))
        page = response.content.decode()
        self.assertLess(page.index('id="json-format-example"'), page.index('Safe source settings only'))
        self.assertLess(page.index('id="json-format-example"'), page.index('id="source-json"'))
        self.assertFalse(Source.objects.exists())

    def test_rejected_upload_shows_copyable_example_before_instructions(self):
        from bs4 import BeautifulSoup
        from leads.services.source_import import parse_upload
        response = self.preview(body=b'[]')
        self.assertEqual(response.status_code, 400)
        soup = BeautifulSoup(response.content, 'html.parser')
        alert = soup.select_one('[role=alert]')
        self.assertIsNotNone(alert)
        example_panel = soup.select_one('#json-format-example')
        self.assertIsNotNone(example_panel)
        example = example_panel.select_one('pre code')
        self.assertIsNotNone(example, 'Rejected files retain the always-visible JSON example')
        self.assertTrue(parse_upload(example.get_text().encode('utf-8'))['sources'])
        self.assertIn('Download example JSON', example_panel.get_text())
        self.assertIn('name', example_panel.get_text())
        self.assertIn('url', example_panel.get_text())
        self.assertIn('Preview JSON uploads', example_panel.get_text())
        page = response.content.decode()
        self.assertLess(page.index('role="alert"'), page.index('Safe source settings only'))
        self.assertIsNone(soup.select_one('input[name=preview_token]'))
        self.assertFalse(Source.objects.exists())

    def test_upload_errors_are_bounded_friendly_and_private(self):
        for filename, body in (("sources.txt", b"{}"), ("sources.json", b"x" * 262145), ("sources.json", b"\xff"), ("sources.json", b'{"schema_version":1,"sources":[]}')):
            response = self.preview(body=body, filename=filename)
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "upload", status_code=400)
        response = self.client.post("/sources/import/", {"action": "preview"})
        self.assertEqual(response.status_code, 400)
        response = self.preview(document({"name": "Test", "url": "https://user:UNSAFE_PASSWORD@example.com/team"}))
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "UNSAFE_PASSWORD", status_code=400)
        self.assertContains(response, "Row 1: url", status_code=400)
        self.assertEqual(self.preview(filename="SOURCES.JSON").status_code, 200)
        self.assertFalse(Source.objects.exists())

    def test_tokens_reject_wrong_user_expiry_tampering_and_unsigned_rows(self):
        token = self.preview().context["preview_token"]
        other = get_user_model().objects.create_user(username="other-staff", is_staff=True)
        client = Client()
        client.force_login(other)
        self.assertEqual(client.post("/sources/import/", {"action": "import", "preview_token": token}).status_code, 400)
        for bad in ("", "not-signed", token + "tamper", "x" * 524289):
            self.assertEqual(self.client.post("/sources/import/", {"action": "import", "preview_token": bad, "sources": json.dumps(document())}).status_code, 400)
        with patch("django.core.signing.time.time", return_value=1):
            expired = self.preview().context["preview_token"]
        self.assertEqual(self.client.post("/sources/import/", {"action": "import", "preview_token": expired}).status_code, 400)
        self.assertFalse(Source.objects.exists())

    def test_confirmation_rechecks_newly_existing_source(self):
        token = self.preview().context["preview_token"]
        source = Source.objects.create(name="Independent operator", url=document()["sources"][0]["url"], active=False, approved=False, approval_notes="Keep review")
        before = Source.objects.values().get(pk=source.pk)
        response = self.client.post("/sources/import/", {"action": "import", "preview_token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["report"]["created"], 0)
        self.assertEqual(response.context["report"]["skipped"], 1)
        self.assertEqual(Source.objects.count(), 1)
        self.assertEqual(Source.objects.values().get(pk=source.pk), before)

    def test_preview_escapes_values_and_does_not_keep_upload_in_session(self):
        response = self.preview(document({"name": '<script>alert("name")</script>', "company": "<img src=x onerror=alert(1)>", "url": "https://example.com/team?x=%22&y=2"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert("name")</script>')
        self.assertContains(response, "&lt;img")
        self.assertEqual(set(self.client.session.keys()), {"_auth_user_id", "_auth_user_backend", "_auth_user_hash"})
        token = response.context["preview_token"]
        result = self.client.post("/sources/import/", {"action": "import", "preview_token": token})
        self.assertContains(result, "&lt;script&gt;")
        self.assertNotContains(result, '<script>alert("name")</script>')

    def test_browser_preview_and_confirm_are_separate_and_repeat_safe(self):
        response = self.client.get("/sources/import/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reverse("source_import"), "/sources/import/")
        self.assertContains(self.client.get(reverse("sources")), 'href="/sources/import/"')
        self.assertContains(response, "256 KiB")
        with patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")), patch("leads.services.network.fetch", side_effect=AssertionError("No fetch")):
            response = self.preview()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Source.objects.count(), 0)
            self.assertContains(response, "Would create")
            self.assertContains(response, "/team")
            token = response.context["preview_token"]
            result = self.client.post(reverse("source_import"), {"action": "import", "preview_token": token, "name": "IGNORED", "active": "true"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(Source.objects.count(), 1)
            self.assertFalse(Source.objects.get().active)
            self.assertContains(result, reverse("source_detail", args=[Source.objects.get().pk]))
            again = self.client.post(reverse("source_import"), {"action": "import", "preview_token": token})
            self.assertEqual(again.status_code, 200)
            self.assertEqual(again.context["report"]["created"], 0)
            self.assertEqual(again.context["report"]["skipped"], 1)
        self.assertEqual(Source.objects.get().name, "Example team")
