import json
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from leads.models import Lead, Source
from leads.services.extraction import extract, signature, validate_recipe
from leads.services.structured import MAX_BLOCK, RECIPE, entities
from automation.extraction_benchmark import run
from automation.packs import hints, suggestions
from automation.policy import collection_allowed
from automation.recipes import evaluate, proposals, readiness
from .test_pipeline import AutomationCase


def person(**changes):
    return {'@context': 'https://schema.org', '@type': 'Person', 'name': 'Alex Example',
            'jobTitle': 'Sales Director', 'email': 'alex@example.test',
            'worksFor': {'@type': 'Organization', 'name': 'Example Payments'}, **changes}


def html(*nodes):
    return '<h1>Example Payments</h1>' + ''.join(
        '<script type="application/ld+json">' + json.dumps(node) + '</script>' for node in nodes)


@override_settings(EXTRACTION_PACKS_ENABLED=True)
class ExtractionTests(SimpleTestCase):
    def setUp(self):
        self.source = Source(company='Example Payments', recipe=RECIPE)

    def test_jsonld_person_employer_and_direct_email(self):
        records, _ = extract(html(person()), self.source)
        self.assertEqual([(r['name'], r['email'], r['company']) for r in records],
                         [('Alex Example', 'alex@example.test', 'Example Payments')])
        self.assertIn('[Parsed json-ld direct properties]', records[0]['evidence'])

    def test_disabled_flag_preserves_legacy_and_blocks_structured(self):
        enabled = signature(self.source)
        with override_settings(EXTRACTION_PACKS_ENABLED=False):
            self.assertNotEqual(enabled, signature(self.source))
            self.assertEqual(suggestions([html(person())]), [])
            self.assertEqual(entities(html(person()))[0], [])
            self.assertFalse(collection_allowed(self.source))
            with self.assertRaisesMessage(ValueError, 'Enable EXTRACTION_PACKS_ENABLED'):
                extract(html(person()), self.source)

    def test_recipe_does_not_accept_other_engines_or_mixed_selectors(self):
        for recipe in ({'engine': 'other'}, RECIPE | {'row': 'body'}, {'url': 'https://example.test'}):
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                validate_recipe(recipe)

    def test_nested_author_and_remote_id_do_not_create_employment(self):
        for node in (person(worksFor={'@id': '#org'}), person(worksFor=None),
                     {'@context': 'https://schema.org', '@type': 'Article', 'author': person()}):
            with self.subTest(node=node):
                self.assertEqual(evaluate(html(node), self.source, RECIPE)[0], [])

    def test_graph_context_is_inherited_but_custom_context_is_not_expanded(self):
        node = person(); node.pop('@context')
        self.assertEqual(len(entities(html({'@context': 'https://schema.org', '@graph': [node]}))[0]), 1)
        for context in ('https://attacker.example/context', {'name': 'malicious'}, ['https://schema.org']):
            self.assertEqual(entities(html(person(**{'@context': context})))[0], [])

    def test_libraries_cannot_fetch_remote_contexts(self):
        with patch('socket.socket.connect', side_effect=AssertionError('Network not allowed')), \
             patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP not allowed')):
            self.assertEqual(len(entities(html(person()))[0]), 1)
            self.assertEqual(entities(html(person(**{'@context': 'https://example.test/context'})))[0], [])

    def test_employer_and_description_contacts_are_not_person_channels(self):
        node = person(email=None, description='Email alex@example.test',
                      worksFor={'@type': 'Organization', 'name': 'Example Payments', 'email': 'office@example.test'})
        self.assertEqual(evaluate(html(node), self.source, RECIPE)[0], [])
        self.assertNotIn('@example.test', entities(html(node))[0][0]['evidence'])

    def test_repeated_and_footer_channels_are_removed(self):
        cases = (html(person(email='sales@example.test')),
                 html(person(), person(name='Casey Fixture')),
                 html(person()) + '<footer>alex@example.test</footer>')
        for page in cases:
            with self.subTest(page=page):
                self.assertEqual(evaluate(page, self.source, RECIPE)[0], [])

    def test_repeated_phone_removal_preserves_individual_emails(self):
        rows = evaluate(html(person(telephone='202-555-0100'),
                             person(name='Casey Fixture', email='casey@example.test', telephone='202-555-0100')),
                        self.source, RECIPE)[0]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row['phone'] for row in rows))

    def test_ambiguous_multiple_values_are_not_silently_chosen(self):
        self.assertEqual(evaluate(html(person(email=['a@example.test', 'b@example.test'])), self.source, RECIPE)[0], [])
        self.assertEqual(evaluate(html(person(worksFor=[{'@type': 'Organization', 'name': 'One'},
                                                     {'@type': 'Organization', 'name': 'Two'}])), self.source, RECIPE)[0], [])

    def test_malformed_or_oversized_blocks_prevent_automatic_release(self):
        for invalid in ('{bad', 'x' * (MAX_BLOCK + 1)):
            page = html(person()) + '<script type="application/ld+json">' + invalid + '</script>'
            stats = evaluate(page, self.source, RECIPE)[2]
            self.assertLess(readiness([stats]), 85)

    def test_microdata_meta_values_and_itemref_boundary(self):
        page = (Path(settings.BASE_DIR) / 'examples/extraction/microdata-train.html').read_text()
        self.assertEqual(len(evaluate(page, self.source, RECIPE)[0]), 2)
        page = page.replace('itemscope itemtype="https://schema.org/Person"',
                            'itemref="footer" itemscope itemtype="https://schema.org/Person"')
        self.assertEqual(entities(page)[0], [])

    def test_platform_detection_does_not_match_plain_text_mentions(self):
        self.assertEqual(hints('<p>We use wordpress webflow squarespace.</p>'), [])
        self.assertEqual(hints('<html data-wf-page="fixture"></html>'), ['webflow'])

    def test_platform_hint_without_contact_rows_cannot_release(self):
        page = '<html data-wf-page="fixture"><div class="w-dyn-item">Software product</div></html>'
        for recipe in proposals([page]):
            self.assertLess(readiness([evaluate(page, self.source, recipe)[2]]), 85)

    def test_heldout_fixtures_improve_associations_without_network(self):
        report = run()
        self.assertEqual(sum(case['extraction_packs']['correct'] for case in report['results']), 10)
        self.assertEqual(sum(case['extraction_packs']['false_positive'] for case in report['results']), 0)
        self.assertLess(sum(case['baseline']['correct'] for case in report['results']), 10)


@override_settings(EXTRACTION_PACKS_ENABLED=True, JEV_MODE='mock', JEV_CAPTURE_ENABLED=True)
class PackPipelineTests(AutomationCase):
    def test_disabling_feature_between_validation_and_release_pauses_setup(self):
        self.responses['/team/'] = html(person())
        job = self.candidate().source.automation_job
        self.run_until(job, states=('recipe_ready', 'paused', 'failed'))
        self.assertEqual(job.state, 'recipe_ready', job.message)
        with override_settings(EXTRACTION_PACKS_ENABLED=False):
            self.run_until(job)
        self.assertEqual(job.state, 'paused', job.message)
        self.assertFalse(Lead.objects.exists())

    def test_structured_contacts_wait_for_canary_and_disabled_flag_blocks_collection(self):
        self.responses['/team/'] = html(person(), person(name='Casey Fixture', email='casey@example.test'))
        job = self.candidate().source.automation_job
        self.run_until(job, states=('recipe_released', 'paused', 'failed'))
        self.assertEqual(job.state, 'recipe_released', job.message)
        self.assertEqual(job.current_recipe.recipe, RECIPE)
        self.assertFalse(Lead.objects.exists())
        self.run_until(job)
        self.assertEqual(job.state, 'active', job.message)
        self.assertEqual(Lead.objects.count(), 2)
        job.source.refresh_from_db()
        with override_settings(EXTRACTION_PACKS_ENABLED=False):
            self.assertFalse(collection_allowed(job.source))
        report = json.dumps(job.recon)
        self.assertNotIn('Alex Example', report)
        self.assertNotIn('alex@example.test', report)

    def test_degraded_structured_canary_retains_zero_leads(self):
        self.responses['/team/'] = html(person())
        job = self.candidate().source.automation_job
        self.run_until(job, states=('recipe_released', 'paused', 'failed'))
        self.assertEqual(job.state, 'recipe_released', job.message)
        self.responses['/team/'] = html(person(email=None))
        self.run_until(job)
        self.assertEqual(job.state, 'paused', job.message)
        self.assertFalse(Lead.objects.exists())

    def test_webflow_pack_completes_existing_automatic_pipeline(self):
        self.responses['/team/'] = (Path(settings.BASE_DIR) / 'examples/extraction/webflow-heldout.html').read_text()
        job = self.candidate().source.automation_job
        self.run_until(job)
        self.assertEqual(job.state, 'active', job.message)
        self.assertEqual(job.current_recipe.recipe['row'], '.w-dyn-item')
        self.assertEqual(set(Lead.objects.values_list('name', flat=True)), {'Robin Demo', 'Morgan Sample'})
