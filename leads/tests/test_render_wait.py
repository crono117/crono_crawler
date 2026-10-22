from types import ModuleType
from unittest.mock import patch
from django.test import TestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from leads.forms import SourceForm
from leads.models import Source
from leads.services.network import Response, fetch_browser


class RenderWaitConfigTests(TestCase):
    def test_default_render_wait_ms_is_1500(self):
        source = Source.objects.create(name="Defaults", url="https://defaults.example.org/", approved=True)
        self.assertEqual(source.render_wait_ms, 1500)

    def test_render_wait_form_validation_cap(self):
        data = {
            "name": "Cap check",
            "url": "https://cap.example.org/page",
            "category": "merchant_services",
            "collector": "browser",
            "render_wait_ms": 10001,
            "extractor": "rules",
            "allowed_paths": "/page",
            "interval_hours": 168,
            "delay_seconds": 5,
            "max_pages": 5,
            "max_depth": 1,
            "approved": "on",
            "approval_notes": "ok",
        }
        with patch("leads.forms.public_addresses", return_value=["8.8.8.8"]):
            form = SourceForm(data=data)
            self.assertFalse(form.is_valid())
            self.assertIn("render_wait_ms", form.errors)
        # Lower, valid value passes
        data["render_wait_ms"] = 10000
        with patch("leads.forms.public_addresses", return_value=["8.8.8.8"]):
            form = SourceForm(data=data)
            self.assertTrue(form.is_valid(), form.errors)

    def test_fetch_browser_uses_configured_wait(self):
        # Prepare a fake Playwright implementation injected via sys.modules so the import succeeds.
        waits = []

        class FakePage:
            def __init__(self):
                self.main_frame = object()

            async def goto(self, url, wait_until, timeout):
                return None

            async def wait_for_timeout(self, ms):
                waits.append(ms)

            async def content(self):
                return "<html><body>Hello</body></html>"

        class FakeContext:
            async def new_page(self):
                return FakePage()

            async def route(self, pattern, handler):
                return None

            async def route_web_socket(self, pattern, closer):
                return None

        class FakeBrowser:
            async def new_context(self, **kwargs):
                return FakeContext()

            async def close(self):
                return None

        class FakePW:
            def __init__(self):
                self.chromium = self

            async def launch(self, headless=True):
                return FakeBrowser()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_api = ModuleType("playwright.async_api")
        async def factory():
            return FakePW()
        # The import in fetch_browser is: from playwright.async_api import async_playwright
        fake_api.async_playwright = lambda: FakePW()
        fake_playwright = ModuleType("playwright")

        with patch.dict("sys.modules", {"playwright": fake_playwright, "playwright.async_api": fake_api}):
            with patch("leads.services.network.fetch") as mock_fetch:
                mock_fetch.return_value = Response(
                    "https://wait.example.org/", 200, {"content-type": "text/html"}, b"<html></html>"
                )
                res = fetch_browser("https://wait.example.org/", "TestAgent", lambda url: True, render_wait_ms=4321)
                self.assertEqual(res.status, 200)
                # The wait must exactly use the configured value (bounded by the function).
                self.assertEqual(waits, [4321])


class RenderWaitMigrationDefinitionTests(TestCase):
    def test_migration_adds_field_with_default_1500(self):
        import importlib
        from django.db.migrations import AddField
        module = importlib.import_module("leads.migrations.0006_source_render_wait_ms")
        add_fields = [op for op in module.Migration.operations if isinstance(op, AddField) and op.name == "render_wait_ms"]
        self.assertTrue(add_fields, "Expected an AddField operation for render_wait_ms")
        field = add_fields[0].field
        self.assertEqual(getattr(field, "default", None), 1500)

