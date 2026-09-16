import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from leads.forms import SourceForm
from leads.models import Lead, Observation, Source
from leads.services.extraction import diagnostic_message, extract, signature, validate_recipe
from leads.services.worker import acquire_lease, enqueue, release_lease, tick


class RecipeDiagnosticsTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Synthetic source", url="https://example.com/team/",
            company="Example Payments", approved=True, collector="demo", follow_links=False, max_pages=1)
        self.html = '''<article class="profile"><div class="person-details">
            <h3>Alex Example</h3><p class="role">Payment sales consultant</p>
            <a href="mailto:alex@example.com">Email Alex</a><a href="tel:202-555-0100">Call Alex</a>
            </div><aside>Payroll products</aside></article>
            <footer><a href="mailto:sales@example.com">Sales office</a></footer>'''

    def inspect(self, html=None):
        diagnostics = {}
        records, _ = extract(html or self.html, self.source, diagnostics=diagnostics)
        return records, diagnostics

    def test_specific_recipe_matches_without_changing_global_selectors(self):
        records, stats = self.inspect()
        self.assertEqual(records, [])
        self.assertEqual(stats["rows_checked"], 0)
        self.assertIn("No person cards matched", diagnostic_message(stats))
        self.source.recipe = {"row": ".profile", "evidence": ".person-details"}
        records, stats = self.inspect()
        self.assertEqual(stats["validated_contacts"], 1)
        self.assertEqual(records[0]["email"], "alex@example.com")
        self.assertNotIn("payroll", records[0]["person_tags"])
        self.assertNotIn("Sales office", records[0]["evidence"])

    def test_named_sales_profiles_do_not_inherit_footer_contacts(self):
        self.source.recipe = {"row": ".profile"}
        html = '<article class="profile"><h3>Alex Example</h3><p class="role">Sales director</p></article><footer><a href="mailto:sales@example.com">Office</a></footer>'
        records, stats = self.inspect(html)
        self.assertEqual(records, [])
        self.assertEqual(stats["rejected"], {"missing_contact": 1})

    def test_narrow_evidence_requires_both_name_and_contact(self):
        self.source.recipe = {"row": ".profile", "evidence": ".person-details"}
        html = '<article class="profile"><div class="person-details"><h3>Alex Example</h3><p class="role">Sales</p></div><a href="mailto:office@example.com">Office</a></article>'
        records, stats = self.inspect(html)
        self.assertEqual(records, [])
        self.assertEqual(stats["rejected"], {"contact_without_evidence": 1})
        self.source.recipe["evidence"] = ".missing"
        records, stats = self.inspect(html)
        self.assertEqual(records, [])
        self.assertEqual(stats["rejected"], {"missing_evidence_container": 1})

    def test_diagnostics_distinguish_role_and_name_rejections(self):
        self.source.recipe = {"row": ".profile"}
        records, stats = self.inspect(self.html.replace("Payment sales consultant", "Software engineer"))
        self.assertEqual(records, [])
        self.assertEqual(stats["rejected"], {"non_sales_role": 1})
        records, stats = self.inspect(self.html.replace("Alex Example", "Alex"))
        self.assertEqual(records, [])
        self.assertEqual(stats["rejected"], {"invalid_name": 1})

    def test_recipe_validation_and_signature_include_evidence(self):
        previous = signature(self.source)
        self.source.recipe = {"row": ".profile", "evidence": ".person-details"}
        self.assertNotEqual(signature(self.source), previous)
        form = SourceForm()
        form.cleaned_data = {"recipe": self.source.recipe}
        self.assertEqual(form.clean_recipe(), self.source.recipe)
        for recipe in ({"row": " "}, {"evidence": "["}, {"unknown": "div"}, {"email": 3}):
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                validate_recipe(recipe)

    def test_preview_is_offline_and_read_only_and_records_require_opt_in(self):
        self.source.extractor = "ollama"
        self.source.save()
        with TemporaryDirectory() as directory:
            html = Path(directory) / "page.html"
            recipe = Path(directory) / "recipe.json"
            html.write_text(self.html)
            recipe.write_text(json.dumps({"row": ".profile", "evidence": ".person-details"}))
            with patch("leads.services.extraction.httpx.Client", side_effect=AssertionError("No model requests")), patch("leads.services.network.fetch", side_effect=AssertionError("No fetches")):
                output = StringIO()
                call_command("inspect_recipe", source=self.source.pk, html=str(html), recipe=str(recipe), stdout=output)
                report = json.loads(output.getvalue())
                self.assertEqual(report["diagnostics"]["validated_contacts"], 1)
                self.assertNotIn("records", report)
                output = StringIO()
                call_command("inspect_recipe", source=self.source.pk, html=str(html), recipe=str(recipe), show_records=True, stdout=output)
                self.assertEqual(json.loads(output.getvalue())["records"][0]["name"], "Alex Example")
        self.source.refresh_from_db()
        self.assertEqual(self.source.recipe, {})
        self.assertEqual(self.source.extractor, "ollama")
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(Observation.objects.exists())

    def test_preview_rejects_invalid_recipe_before_extraction(self):
        with TemporaryDirectory() as directory:
            html, recipe = Path(directory) / "page.html", Path(directory) / "recipe.json"
            html.write_text(self.html)
            recipe.write_text('{"evidence": "["}')
            with self.assertRaises(CommandError):
                call_command("inspect_recipe", source=self.source.pk, html=str(html), recipe=str(recipe))

    def test_regular_worker_reports_zero_card_diagnostics(self):
        self.source.recipe = {"row": ".missing"}
        self.source.save()
        run = enqueue(self.source)
        token = acquire_lease()
        try:
            tick(token)
        finally:
            release_lease(token)
        run.refresh_from_db()
        self.assertTrue(run.needs_recipe_review)
        self.assertIn("No person cards matched", run.jobs.get().message)
