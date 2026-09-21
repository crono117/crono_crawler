import hashlib
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from automation.tests.test_extraction_packs import html, person
from classification.accounting import valid_evidence
from classification.evidence import capture_page, purge_expired, valid_span
from classification.models import Affiliation, ContactCandidate, EvidenceDocument, EvidenceSpan, Person
from leads.models import Lead, Source


@override_settings(EXTRACTION_PACKS_ENABLED=True, JEV_MODE='mock')
class StructuredEvidenceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name='Synthetic structured test', company='Example Payments',
            url='https://example.test/team/', approved=True, allowed_paths='/team/')

    def capture(self, page):
        return capture_page(self.source, self.source.url, hashlib.sha256(page.encode()).hexdigest(), page)

    def test_structured_engineer_without_channel_is_retained_before_lead_filter(self):
        self.capture(html(person(name='Jordan Example', jobTitle='Software Engineer', email=None)))
        self.assertTrue(Person.objects.filter(name='Jordan Example').exists())
        self.assertFalse(ContactCandidate.objects.exists())
        self.assertFalse(Lead.objects.exists())
        self.assertEqual(Affiliation.objects.get().title, 'Software Engineer')

    def test_evidence_provenance_hashes_offsets_cache_and_flag_invalidation(self):
        page = html(person())
        evaluations = self.capture(page)
        self.assertTrue(evaluations)
        self.assertTrue(all(valid_span(span) for span in EvidenceSpan.objects.select_related('document')))
        doc = EvidenceDocument.objects.get()
        self.assertEqual(doc.provenance, 'fetched+structured-v1')
        self.assertEqual(doc.content_hash, hashlib.sha256(page.encode()).hexdigest())
        self.assertIn('json-ld:', EvidenceSpan.objects.get(kind='person').locator)
        self.assertTrue(valid_evidence(evaluations[-1]))
        self.capture(page)
        self.assertEqual(EvidenceDocument.objects.count(), 1)
        with override_settings(EXTRACTION_PACKS_ENABLED=False):
            self.assertFalse(valid_evidence(evaluations[-1]))

    def test_missing_employer_does_not_create_affiliation(self):
        self.capture(html(person(worksFor=None)))
        self.assertFalse(Person.objects.exists())
        self.assertFalse(Affiliation.objects.exists())

    def test_organization_and_person_on_json_only_page(self):
        page = html({'@context': 'https://schema.org', '@type': 'Organization', 'name': 'Example Payments'}, person())
        self.capture(page.replace('<h1>Example Payments</h1>', ''))
        self.assertEqual(Person.objects.get().name, 'Alex Example')
        self.assertTrue(EvidenceSpan.objects.get(kind='company').locator.startswith('json-ld:'))

    def test_shared_footer_and_nested_employer_email_do_not_attach_to_person(self):
        self.capture(html(person(email=None, worksFor={'@type':'Organization', 'name':'Example Payments',
            'email':'owner@example.test'})) + '<footer>support@example.test</footer>')
        self.assertFalse(ContactCandidate.objects.filter(person__isnull=False).exists())
        self.assertFalse(ContactCandidate.objects.filter(value='owner@example.test').exists())
        self.assertTrue(ContactCandidate.objects.get().shared_hint)

    def test_microdata_itemref_cannot_bypass_adapter_through_legacy_capture(self):
        self.capture('<h1>Example Payments</h1><div itemscope itemtype="https://schema.org/Person" itemref="footer">'
            '<b itemprop="name">Alex Example</b><span itemprop="jobTitle">Sales Director</span>'
            '<a href="mailto:alex@example.test">Email</a></div>')
        self.assertFalse(Person.objects.exists())

    def test_scope_and_source_approval_still_required(self):
        page = html(person())
        self.assertEqual(capture_page(self.source, 'https://example.test/private/', 'hash', page), [])
        self.source.approved = False; self.source.save()
        self.assertEqual(self.capture(page), [])
        self.assertFalse(EvidenceDocument.objects.exists())

    def test_retention_removes_structured_contact_values(self):
        self.capture(html(person()))
        self.assertTrue(ContactCandidate.objects.exists())
        EvidenceDocument.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        purge_expired()
        self.assertEqual(EvidenceDocument.objects.get().text, '')
        self.assertFalse(ContactCandidate.objects.exists())
