import hashlib
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from classification.evidence import capture_page
from classification.models import Company, CompanyJob, EvidenceSpan, Person, Pilot
from discovery.models import Campaign, DiscoveryRun
from leads.models import Source
from leads.services.extraction import contact_evidence, soup_for

HEADING = '<h1>Example Payments</h1><p>Merchant services and POS for local business.</p>'
EMPLOYEE = '''<section class="employee-bio"><h2>Alex Example</h2><p>Sales Director</p>
<p>alex@example.test</p></section>'''
LONG_NAV = '<nav>' + ''.join(f'<a href="/p{i}/">Product category number {i}</a>' for i in range(60)) + '</nav>'


def block_questions(evaluations):
    return [key for evaluation in evaluations for key in evaluation.request['questions']
            if key.startswith('candidate_block_')]


@override_settings(JEV_MODE='mock', JEV_CAPTURE_ENABLED=True, JEV_LAYERED_BLOCKS_ENABLED=True,
                   JEV_TOKEN_COUNTER='')
class FallbackEligibilityTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name='Yield fixture', company='Example Payments',
                                            url='https://example.test/team/', approved=True,
                                            allowed_paths='/team/')

    def capture(self, page, **kwargs):
        return capture_page(self.source, self.source.url, hashlib.sha256(page.encode()).hexdigest(), page, **kwargs)

    def spans(self):
        return [span.text for span in EvidenceSpan.objects.filter(kind='candidate_block').select_related('document')]

    def test_junk_css_row_does_not_suppress_valid_fallback_profile(self):
        junk = '<div class="team-member"><h3>Team</h3></div>'
        evaluations = self.capture(f'<html><body>{HEADING}{junk}{EMPLOYEE}</body></html>')

        self.assertEqual(len(block_questions(evaluations)), 1)
        self.assertEqual(len(self.spans()), 1)
        self.assertIn('Alex Example', self.spans()[0])
        self.assertFalse(Person.objects.exists())

    @override_settings(EXTRACTION_PACKS_ENABLED=True)
    def test_unrelated_schema_author_does_not_suppress_valid_fallback_profile(self):
        author = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Person",'
                  '"name":"Blog Author"}</script>')
        evaluations = self.capture(f'<html><head>{author}</head><body>{HEADING}{EMPLOYEE}</body></html>')

        self.assertEqual(len(block_questions(evaluations)), 1)
        self.assertIn('Alex Example', self.spans()[0])
        # A bare Person without an employer is still never captured as an affiliated person.
        self.assertFalse(Person.objects.exists())

    @override_settings(EXTRACTION_PACKS_ENABLED=True)
    def test_structured_person_covers_its_own_block_without_duplicate(self):
        person = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Person",'
                  '"name":"Alex Example","jobTitle":"Sales Director","email":"alex@example.test",'
                  '"worksFor":{"@type":"Organization","name":"Example Payments"}}</script>')
        evaluations = self.capture(f'<html><head>{person}</head><body>{HEADING}{EMPLOYEE}</body></html>')

        self.assertEqual(list(Person.objects.values_list('name', flat=True)), ['Alex Example'])
        self.assertEqual(block_questions(evaluations), [])
        self.assertFalse(EvidenceSpan.objects.filter(kind='candidate_block').exists())

    def test_mixed_layout_keeps_css_person_and_blocks_only_the_uncovered_profile(self):
        card = ('<div class="team-member"><h3>Casey Sample</h3><p class="role">Sales Manager</p>'
                '<a href="mailto:casey@example.test">Email</a></div>')
        evaluations = self.capture(f'<html><body>{HEADING}{card}{EMPLOYEE}</body></html>')

        self.assertEqual(list(Person.objects.values_list('name', flat=True)), ['Casey Sample'])
        self.assertEqual(len(block_questions(evaluations)), 1)
        spans = self.spans()
        self.assertEqual(len(spans), 1)
        self.assertIn('Alex Example', spans[0])
        self.assertNotIn('Casey Sample', spans[0])

    def test_css_captured_card_is_not_duplicated_as_a_block(self):
        card = ('<section><h2>Our sales team</h2><div class="team-member"><h3>Casey Sample</h3>'
                '<p class="role">Sales Manager</p><p>casey@example.test</p></div></section>')
        evaluations = self.capture(f'<html><body>{HEADING}{card}</body></html>')

        self.assertEqual(list(Person.objects.values_list('name', flat=True)), ['Casey Sample'])
        self.assertEqual(block_questions(evaluations), [])

    def test_existing_block_limits_still_apply_when_css_rows_also_match(self):
        junk = '<div class="team-member"><h3>Team</h3></div>'
        cards = ''.join(f'''<section class="employee-bio"><h2>Person Number {index}</h2>
                <p>Vice President of Sales</p><p>person{index}@example.test</p></section>''' for index in range(9))
        evaluations = self.capture(f'<html><body>{HEADING}{junk}{cards}</body></html>')

        self.assertEqual(len(block_questions(evaluations)), 6)

    def test_company_job_capture_still_skips_layered_blocks(self):
        campaign = Campaign.objects.create(name='Bounded company work')
        campaign.sources.add(self.source)
        expires = timezone.now() + timedelta(days=1)
        pilot = Pilot.objects.create(name='Pilot', campaign=campaign, expires_at=expires)
        company = Company.objects.create(identity='company-job-yield', name='Example Payments')
        job = CompanyJob.objects.create(company=company, pilot=pilot, source=self.source,
                                        run=DiscoveryRun.objects.create(campaign=campaign),
                                        key='company-job-yield', expires_at=expires)
        junk = '<div class="team-member"><h3>Team</h3></div>'
        evaluations = self.capture(f'<html><body>{HEADING}{junk}{EMPLOYEE}</body></html>', company_job=job)

        self.assertEqual(block_questions(evaluations), [])

    @override_settings(JEV_LAYERED_BLOCKS_ENABLED=False)
    def test_layered_flag_off_still_adds_no_blocks(self):
        junk = '<div class="team-member"><h3>Team</h3></div>'
        evaluations = self.capture(f'<html><body>{HEADING}{junk}{EMPLOYEE}</body></html>')

        self.assertEqual(block_questions(evaluations), [])


@override_settings(JEV_MODE='mock', JEV_CAPTURE_ENABLED=True, JEV_LAYERED_BLOCKS_ENABLED=True,
                   JEV_TOKEN_COUNTER='')
class CompanyPassageTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name='Passage fixture', company='Example Payments',
                                            url='https://example.test/team/', approved=True,
                                            allowed_paths='/team/')

    def capture(self, page):
        return capture_page(self.source, self.source.url, hashlib.sha256(page.encode()).hexdigest(), page)

    def test_long_navigation_does_not_stop_profile_capture(self):
        page = f'<html><body>{LONG_NAV}{HEADING}{EMPLOYEE}</body></html>'
        self.assertGreater(contact_evidence(soup_for(page)).find('Example Payments'), 1200)

        evaluations = self.capture(page)

        self.assertEqual(len(evaluations), 2)
        self.assertEqual(len(block_questions(evaluations)), 1)
        company_span = EvidenceSpan.objects.get(kind='company')
        self.assertIn('Example Payments', company_span.text)
        self.assertLessEqual(len(company_span.text), 1200)
        self.assertEqual(company_span.locator, 'company-mention')
        self.assertEqual(company_span.document.text[company_span.start:company_span.end], company_span.text)

    def test_long_navigation_does_not_stop_css_person_capture(self):
        card = ('<div class="team-member"><h3>Casey Sample</h3><p class="role">Sales Manager</p>'
                '<a href="mailto:casey@example.test">Email</a></div>')
        self.capture(f'<html><body>{LONG_NAV}{HEADING}{card}</body></html>')

        self.assertEqual(list(Person.objects.values_list('name', flat=True)), ['Casey Sample'])

    def test_company_in_page_opening_keeps_existing_passage(self):
        page = f'<html><body>{HEADING}{EMPLOYEE}</body></html>'
        self.capture(page)

        company_span = EvidenceSpan.objects.get(kind='company')
        self.assertEqual(company_span.key, 'company')
        self.assertEqual(company_span.locator, 'page-opening')
        self.assertEqual(company_span.text, contact_evidence(soup_for(page))[:1200])

    def test_missing_company_evidence_never_creates_an_affiliation(self):
        self.source.company = 'Absent Holdings'
        self.source.save(update_fields=['company'])
        card = ('<div class="team-member"><h3>Casey Sample</h3><p class="role">Sales Manager</p>'
                '<a href="mailto:casey@example.test">Email</a></div>')

        evaluations = self.capture(f'<html><body><p>Welcome</p>{card}{EMPLOYEE}</body></html>')

        self.assertEqual(evaluations, [])
        self.assertFalse(Company.objects.exists())
        self.assertFalse(Person.objects.exists())
