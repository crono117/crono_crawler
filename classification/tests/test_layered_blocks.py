import hashlib
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from automation.models import RecipeVersion
from automation.policy import scope_hash
from classification.contracts import digest
from classification.evidence import VERSION as EVIDENCE_VERSION, candidate_blocks, capture_page
from classification.models import (Affiliation, Company, CompanyJob, ContactCandidate, Evaluation,
                                   EvidenceDocument, EvidenceSpan, Person, Pilot)
from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from leads.models import Lead, Source
from leads.services.extraction import EMAIL, signature, soup_for


@override_settings(
    JEV_MODE='mock',
    JEV_CAPTURE_ENABLED=True,
    JEV_LAYERED_BLOCKS_ENABLED=True,
    JEV_TOKEN_COUNTER='',
)
class LayeredBlockEvidenceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            name='Layered fixture',
            company='Example Payments',
            url='https://example.test/team/',
            approved=True,
            allowed_paths='/team/',
        )

    def capture(self, page, **kwargs):
        return capture_page(
            self.source,
            self.source.url,
            hashlib.sha256(page.encode()).hexdigest(),
            page,
            **kwargs,
        )

    def test_zero_css_employee_bio_queues_review_only_page_block_evaluation(self):
        page = '''
            <html><body>
              <h1>Example Payments</h1>
              <section class="leadership-bio">
                <h2>Alex Example</h2>
                <p>Vice President of Sales</p>
                <a href="mailto:alex@example.test">Email Alex</a>
              </section>
            </body></html>
        '''

        evaluations = self.capture(page)

        layered = Evaluation.objects.get(request__questions__has_key='candidate_block_b1')
        self.assertIn(layered, evaluations)
        self.assertEqual(set(layered.request['questions']), {'page_purpose', 'candidate_block_b1'})
        self.assertEqual(layered.request['state']['company'], 'Example Payments')
        self.assertEqual(len(layered.request['state']['blocks']), 1)
        block = layered.request['state']['blocks'][0]
        self.assertEqual(block['id'], 'b1')
        self.assertEqual(block['span_id'], layered.bindings['candidate_block_b1'][0])
        self.assertIn('Alex Example', block['text'])
        self.assertIn('Vice President of Sales', block['text'])
        self.assertIn('alex@example.test', block['text'])
        self.assertFalse(Person.objects.exists())
        self.assertFalse(Affiliation.objects.exists())
        self.assertFalse(ContactCandidate.objects.exists())
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(RecipeVersion.objects.exists())
        self.assertFalse(DiscoveredURL.objects.exists())
        self.assertFalse(DiscoveryJob.objects.exists())
        self.assertEqual(EvidenceSpan.objects.filter(kind='candidate_block').count(), 1)

    def test_candidate_blocks_exclude_regions_deduplicate_and_preserve_signals_within_caps(self):
        hidden = '''<h2>Hidden Person</h2><p>Sales Director</p>
                    <a href="mailto:hidden@example.test">Email</a>'''
        duplicate = '''<section><h2>Alex Example</h2><p>Sales Director</p>
                       <a href="mailto:alex@example.test">Email</a></section>'''
        long_blocks = ''.join(
            f'''<article><h2>Person Number {index}</h2><p>Vice President of Sales {'x' * 520}</p>
                <a href="mailto:person{index}@example.test">Email</a></article>'''
            for index in range(8)
        )
        page = f'''<header>{hidden}</header><nav>{hidden}</nav><footer>{hidden}</footer>
                   <form>{hidden}</form><dialog>{hidden}</dialog><template>{hidden}</template>
                   <div hidden>{hidden}</div><div aria-hidden="true">{hidden}</div>
                   <div style="display: none">{hidden}</div>
                   <div>{duplicate}</div>{duplicate}{long_blocks}'''

        blocks = candidate_blocks(soup_for(page))

        self.assertLessEqual(len(blocks), 6)
        self.assertLessEqual(sum(len(block['text']) for block in blocks), 2400)
        self.assertTrue(all(len(block['text']) <= 500 for block in blocks))
        self.assertEqual(sum('Alex Example' in block['text'] for block in blocks), 1)
        self.assertFalse(any('Hidden Person' in block['text'] for block in blocks))
        self.assertTrue(all('Sales' in block['text'] and EMAIL.search(block['text']) for block in blocks))

    @override_settings(JEV_LAYERED_BLOCKS_ENABLED=False)
    def test_flag_off_preserves_existing_capture_behavior(self):
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''

        evaluations = self.capture(page)

        self.assertEqual(len(evaluations), 1)
        document = EvidenceDocument.objects.get()
        body_hash = hashlib.sha256(page.encode()).hexdigest()
        self.assertEqual(document.fingerprint, digest([
            self.source.pk, self.source.url, body_hash, scope_hash(self.source),
            signature(self.source), EVIDENCE_VERSION]))
        self.assertEqual(evaluations[0].catalog_version, 'jev-leads-v1.0.0')
        self.assertFalse(EvidenceSpan.objects.filter(kind='candidate_block').exists())
        self.assertFalse(Evaluation.objects.filter(request__questions__has_key='candidate_block_b1').exists())

    def test_deterministic_css_rows_skip_layered_fallback(self):
        self.source.recipe = {'row': '.employee-bio', 'name': 'h2'}
        self.source.save(update_fields=['recipe'])
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''

        self.capture(page)

        self.assertFalse(EvidenceSpan.objects.filter(kind='candidate_block').exists())

    def test_repeated_capture_reuses_layered_document_and_changed_html_is_distinct(self):
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''
        first = self.capture(page)
        second = self.capture(page)
        changed = self.capture(page.replace('Sales Director', 'Vice President of Sales'))

        first_layered = next(item for item in first if 'candidate_block_b1' in item.request['questions'])
        second_layered = next(item for item in second if 'candidate_block_b1' in item.request['questions'])
        changed_layered = next(item for item in changed if 'candidate_block_b1' in item.request['questions'])
        self.assertEqual(first_layered.pk, second_layered.pk)
        self.assertNotEqual(first_layered.document_id, changed_layered.document_id)
        self.assertEqual(first_layered.bindings['candidate_block_b1'],
                         [first_layered.request['state']['blocks'][0]['span_id']])

    def test_enabling_layered_mode_does_not_reuse_flag_off_document_projection(self):
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''
        with self.settings(JEV_LAYERED_BLOCKS_ENABLED=False):
            self.capture(page)

        evaluations = self.capture(page)

        layered = next(item for item in evaluations if 'candidate_block_b1' in item.request['questions'])
        self.assertEqual(EvidenceDocument.objects.count(), 2)
        self.assertIn('[Candidate block b1]', layered.document.text)

    def test_mock_block_judgment_never_promotes_people_contacts_or_leads(self):
        from classification import services
        from leads.services.worker import acquire_lease, release_lease
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''
        layered = next(item for item in self.capture(page)
                       if 'candidate_block_b1' in item.request['questions'])
        token = acquire_lease()
        try:
            self.assertTrue(services.process(layered, token, mock_only=True))
        finally:
            release_lease(token)
        layered.refresh_from_db()

        self.assertEqual(layered.state, 'succeeded')
        self.assertTrue(layered.judgments.filter(question_id='candidate_block_b1', label='person_profile').exists())
        self.assertFalse(Person.objects.exists())
        self.assertFalse(ContactCandidate.objects.exists())
        self.assertFalse(Lead.objects.exists())

    def test_structured_person_skips_layered_fallback(self):
        page = '''<html><head><script type="application/ld+json">{
          "@context":"https://schema.org", "@type":"Person", "name":"Alex Example",
          "jobTitle":"Sales Director", "email":"alex@example.test",
          "worksFor":{"@type":"Organization", "name":"Example Payments"}
        }</script></head><body><h1>Example Payments</h1></body></html>'''

        self.capture(page)

        self.assertFalse(EvidenceSpan.objects.filter(kind='candidate_block').exists())

    def test_company_job_capture_does_not_add_layered_evaluation(self):
        campaign = Campaign.objects.create(name='Bounded company work')
        campaign.sources.add(self.source)
        expires = timezone.now() + timedelta(days=1)
        pilot = Pilot.objects.create(name='Pilot', campaign=campaign, expires_at=expires)
        company = Company.objects.create(identity='company-job-fixture', name='Example Payments')
        run = DiscoveryRun.objects.create(campaign=campaign)
        job = CompanyJob.objects.create(company=company, pilot=pilot, source=self.source, run=run,
                                        key='company-job-fixture', expires_at=expires)
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''

        evaluations = self.capture(page, company_job=job)

        self.assertFalse(any('candidate_block_b1' in item.request['questions'] for item in evaluations))
        self.assertFalse(EvidenceSpan.objects.filter(kind='candidate_block').exists())

    def test_unsupplied_block_id_fails_request_contract_validation(self):
        import copy
        from classification import contracts
        page = '''<h1>Example Payments</h1><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''
        layered = next(item for item in self.capture(page)
                       if 'candidate_block_b1' in item.request['questions'])
        request = copy.deepcopy(layered.request)
        request['questions']['candidate_block_b999'] = request['questions'].pop('candidate_block_b1')

        with self.assertRaises(contracts.ContractError):
            contracts.validate_request(request)

    def test_long_page_reserves_document_space_for_exact_candidate_spans(self):
        page = '''<h1>Example Payments</h1><p>{}</p><section class="employee-bio">
                  <h2>Alex Example</h2><p>Sales Director</p>
                  <a href="mailto:alex@example.test">Email</a></section>'''.format('x' * 51000)

        evaluations = self.capture(page)

        layered = next(item for item in evaluations if 'candidate_block_b1' in item.request['questions'])
        span = EvidenceSpan.objects.get(pk=layered.bindings['candidate_block_b1'][0])
        self.assertLessEqual(len(layered.document.text), 50000)
        self.assertIn('Alex Example', span.text)
        self.assertEqual(span.text, layered.request['state']['blocks'][0]['text'])

    def test_split_page_purpose_binding_contains_only_supplied_block_spans(self):
        cards = ''.join(
            f'''<section class="employee-bio"><h2>Person Number {index}</h2>
                <p>Vice President of Sales</p>
                <a href="mailto:person{index}@example.test">Email</a></section>'''
            for index in range(6)
        )
        evaluations = self.capture(f'<h1>Example Payments</h1>{cards}')

        page_evaluation = next(item for item in evaluations if 'page_purpose' in item.request['questions']
                               and item.request['state'].get('blocks'))
        supplied_span_ids = [block['span_id'] for block in page_evaluation.request['state']['blocks']]
        bound_ids = page_evaluation.bindings['page_purpose']
        self.assertEqual(bound_ids[1:], supplied_span_ids)
        self.assertEqual(EvidenceSpan.objects.get(pk=bound_ids[0]).kind, 'company')
