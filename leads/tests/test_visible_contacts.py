from django.test import TestCase

from automation.recipes import evaluate, proposals
from leads.models import Source
from leads.services.extraction import extract


def page(*cards, footer=''):
    return f'<html><body><h1>Example Payments</h1>{"".join(cards)}{footer}</body></html>'


def card(name, body, cls='team-member'):
    return f'<div class="{cls}"><h3>{name}</h3><p class="role">Sales Manager</p>{body}</div>'


class VisibleContactTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name='Visible contact fixture', company='Example Payments',
                                            url='https://example.test/team/', approved=True)

    def records(self, html, recipe=None):
        self.source.recipe = recipe or {}
        return extract(html, self.source)[0]

    def test_plain_text_email_in_email_element_is_selected_by_default(self):
        records = self.records(page(card('Alex Example', '<span class="email">alex@example.test</span>')))

        self.assertEqual([(r['name'], r['email']) for r in records], [('Alex Example', 'alex@example.test')])
        self.assertIn('alex@example.test', records[0]['evidence'])

    def test_labelled_plain_text_email_keeps_only_the_address(self):
        records = self.records(page(card('Alex Example', '<span class="email">Email: alex@example.test</span>')))

        self.assertEqual(records[0]['email'], 'alex@example.test')

    def test_unlabelled_single_email_inside_card_is_used(self):
        records = self.records(page(card('Alex Example', '<p>alex@example.test</p>')))

        self.assertEqual(records[0]['email'], 'alex@example.test')

    def test_plain_text_phone_inside_card_is_used_exactly(self):
        records = self.records(page(card('Alex Example', '<p>Direct line: (555) 010-1234</p>')))

        self.assertEqual(records[0]['phone'], '(555) 010-1234')
        self.assertEqual(records[0]['email'], '')

    def test_contact_label_without_address_does_not_block_card_fallback(self):
        body = '<span class="phone">Phone</span><p>(555) 010-1234</p>'
        records = self.records(page(card('Alex Example', body)))

        self.assertEqual(records[0]['phone'], '(555) 010-1234')

    def test_explicit_phone_recipe_keeps_non_us_text_format(self):
        html = page(card('Alex Example', '<span class="direct">020 7946 0000</span>'))
        records = self.records(html, {'phone': '.direct'})

        self.assertEqual(records[0]['phone'], '020 7946 0000')

    def test_two_different_visible_emails_are_ambiguous(self):
        diagnostics = {}
        self.source.recipe = {}
        records, _ = extract(page(card('Alex Example', '<p>alex@example.test</p><p>jordan@example.test</p>')),
                             self.source, diagnostics=diagnostics)

        self.assertEqual(records, [])
        self.assertEqual(diagnostics['rejected'], {'missing_contact': 1})

    def test_two_names_in_one_card_make_visible_contact_ambiguous(self):
        body = '<h3>Jordan Sample</h3><p>alex@example.test</p>'
        self.assertEqual(self.records(page(card('Alex Example', body))), [])

    def test_selected_mailto_wins_over_other_visible_text(self):
        body = '<a href="mailto:alex@example.test">Email</a><p>assistant@example.test</p>'
        records = self.records(page(card('Alex Example', body)))

        self.assertEqual(records[0]['email'], 'alex@example.test')

    def test_page_footer_contact_repeated_in_card_is_not_assigned(self):
        footer = '<footer>Call us: (555) 010-9999 frontdesk@examplepayments.test</footer>'
        body = '<p>Office: (555) 010-9999</p><p>frontdesk@examplepayments.test</p>'
        self.assertEqual(self.records(page(card('Alex Example', body), footer=footer)), [])

    def test_nested_footer_inside_card_is_not_used(self):
        body = '<footer><p>desk@example.test</p></footer>'
        self.assertEqual(self.records(page(card('Alex Example', body))), [])

    def test_generic_visible_inbox_is_not_assigned_to_a_person(self):
        self.assertEqual(self.records(page(card('Alex Example', '<p>info@example.test</p>'))), [])

    def test_visible_switchboard_shared_by_cards_is_not_assigned(self):
        records = self.records(page(
            card('Alex Example', '<p>alex@example.test</p><p>(555) 010-1000</p>'),
            card('Jordan Sample', '<p>jordan@example.test</p><p>(555) 010-1000</p>')))

        self.assertEqual(sorted((r['name'], r['email'], r['phone']) for r in records),
                         [('Alex Example', 'alex@example.test', ''), ('Jordan Sample', 'jordan@example.test', '')])

    def test_mixed_link_and_visible_cards_produce_one_record_each(self):
        records = self.records(page(
            card('Alex Example', '<a href="mailto:alex@example.test">Email Alex</a>'),
            card('Jordan Sample', '<span class="email">jordan@example.test</span>')))

        self.assertEqual(sorted((r['name'], r['email']) for r in records),
                         [('Alex Example', 'alex@example.test'), ('Jordan Sample', 'jordan@example.test')])
        self.assertTrue(all(r['contact_scope'] == 'unknown' for r in records))

    def test_same_address_as_link_and_text_is_one_direct_contact(self):
        body = '<a href="mailto:alex@example.test">alex@example.test</a>'
        records = self.records(page(card('Alex Example', body)))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['contact_scope'], 'unknown')

    def test_generated_proposals_select_visible_contacts(self):
        html = page(card('Alex Example', '<span class="email">alex@example.test</span>'))
        best = 0
        for recipe in proposals([html]):
            try:
                best = max(best, len(evaluate(html, self.source, recipe)[0]))
            except ValueError:
                continue

        self.assertEqual(best, 1)

    def test_local_validation_rejects_footer_contact_in_card(self):
        footer = '<footer>frontdesk@examplepayments.test</footer>'
        html = page(card('Alex Example', '<p>frontdesk@examplepayments.test</p>'), footer=footer)
        records, _, stats = evaluate(html, self.source, {'row': '.team-member', 'name': 'h3'})

        self.assertEqual(records, [])
        self.assertEqual(stats['primary_rejections'], {'shared_or_global_contact': 1})

    def test_local_validation_accepts_plain_text_card_contact(self):
        html = page(card('Alex Example', '<p>alex@example.test</p>'))
        records, _, _ = evaluate(html, self.source, {'row': '.team-member', 'name': 'h3'})

        self.assertEqual([r['email'] for r in records], ['alex@example.test'])


class SelectedVisibleContactSafeguardTests(TestCase):
    """The same safeguards apply whether the visible address is wrapped or bare."""
    EMAIL_WRAPPERS = ('<span class="email">{}</span>', '<span itemprop="email">{}</span>', '<p>{}</p>')
    PHONE_WRAPPERS = ('<span class="phone">{}</span>', '<span itemprop="telephone">{}</span>', '<p>{}</p>')

    def setUp(self):
        self.source = Source.objects.create(name='Safeguard fixture', company='Example Payments',
                                            url='https://example.test/team/', approved=True)

    def outcomes(self, html):
        self.source.recipe = {}
        ordinary = extract(html, self.source)[0]
        local = evaluate(html, self.source, {'row': '.team-member', 'name': 'h3'})[0]
        generated = []
        for recipe in proposals([html]):
            try:
                generated.extend(evaluate(html, self.source, recipe)[0])
            except ValueError:
                continue
        return {'ordinary': sorted((r['name'], r['email'], r['phone']) for r in ordinary),
                'local': sorted((r['name'], r['email'], r['phone']) for r in local),
                'generated': sorted({(r['name'], r['email'], r['phone']) for r in generated})}

    def assert_none(self, html):
        self.assertEqual(self.outcomes(html), {'ordinary': [], 'local': [], 'generated': []})

    def test_two_addresses_in_one_element_are_ambiguous(self):
        for wrapper in self.EMAIL_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assert_none(page(card('Alex Example', wrapper.format('alex@example.test jordan@example.test'))))
        for wrapper in self.PHONE_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assert_none(page(card('Alex Example', wrapper.format('(555) 010-1234 (555) 010-5678'))))

    def test_second_person_heading_blocks_assignment(self):
        for wrapper in self.EMAIL_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assert_none(page(card('Alex Example', '<h3>Jordan Sample</h3>' + wrapper.format('jordan@example.test'))))

    def test_nested_footer_address_is_ignored(self):
        for wrapper in self.EMAIL_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assert_none(page(card('Alex Example', '<footer>' + wrapper.format('frontdesk@example.test') + '</footer>')))
        for wrapper in self.PHONE_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assert_none(page(card('Alex Example', '<footer>' + wrapper.format('(555) 010-7777') + '</footer>')))

    def test_generic_inbox_is_not_assigned(self):
        for wrapper in self.EMAIL_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assert_none(page(card('Alex Example', wrapper.format('info@example.test'))))

    def test_page_footer_address_is_not_assigned(self):
        footer = '<footer>frontdesk@examplepayments.test (555) 010-9999</footer>'
        for email, phone in zip(self.EMAIL_WRAPPERS, self.PHONE_WRAPPERS):
            with self.subTest(wrapper=email):
                body = email.format('frontdesk@examplepayments.test') + phone.format('(555) 010-9999')
                self.assert_none(page(card('Alex Example', body), footer=footer))

    def test_repeated_switchboard_phone_is_dropped_everywhere(self):
        expected = [('Alex Example', 'alex@example.test', ''), ('Jordan Sample', 'jordan@example.test', '')]
        for email, phone in zip(self.EMAIL_WRAPPERS, self.PHONE_WRAPPERS):
            with self.subTest(wrapper=phone):
                html = page(card('Alex Example', email.format('alex@example.test') + phone.format('(555) 010-1000')),
                            card('Jordan Sample', email.format('jordan@example.test') + phone.format('(555) 010-1000')))
                outcome = self.outcomes(html)
                self.assertEqual(outcome['ordinary'], expected)
                self.assertEqual(outcome['local'], expected)
                self.assertEqual(outcome['generated'], expected)

    def test_single_wrapped_or_bare_address_is_accepted_everywhere(self):
        for email, phone in zip(self.EMAIL_WRAPPERS, self.PHONE_WRAPPERS):
            with self.subTest(wrapper=email):
                html = page(card('Alex Example', email.format('alex@example.test') + phone.format('(555) 010-1234')))
                expected = [('Alex Example', 'alex@example.test', '(555) 010-1234')]
                self.assertEqual(self.outcomes(html), {'ordinary': expected, 'local': expected, 'generated': expected})


class ExplicitSelectorTests(TestCase):
    """An operator's explicit selector keeps its own selection; nothing is inferred around it."""

    def setUp(self):
        self.source = Source.objects.create(name='Explicit fixture', company='Example Payments',
                                            url='https://example.test/team/', approved=True)

    def records(self, html, recipe):
        self.source.recipe = recipe
        return [(r['name'], r['email'], r['phone']) for r in extract(html, self.source)[0]]

    def test_explicit_selector_for_an_exact_address_still_works(self):
        html = page(card('Alex Example', '<span class="email">alex@example.test</span>'))
        self.assertEqual(self.records(html, {'email': '.email'}), [('Alex Example', 'alex@example.test', '')])

    def test_explicit_selector_does_not_pick_one_of_several_addresses(self):
        html = page(card('Alex Example', '<span class="email">alex@example.test jordan@example.test</span>'))
        self.assertEqual(self.records(html, {'email': '.email'}), [])

    def test_empty_explicit_selector_disables_email_inference(self):
        html = page(card('Alex Example', '<p>alex@example.test</p><a href="tel:+15550101234">Call</a>'))
        self.assertEqual(self.records(html, {'email': ''}), [('Alex Example', '', '+15550101234')])

    def test_non_matching_explicit_selector_is_not_replaced_by_inference(self):
        html = page(card('Alex Example', '<p>alex@example.test</p>'))
        self.assertEqual(self.records(html, {'email': '.work-email'}), [])

    def test_legacy_generated_link_selector_allows_inference(self):
        html = page(card('Alex Example', '<p>alex@example.test</p>'))
        self.assertEqual(self.records(html, {'row': '.team-member', 'email': "a[href^='mailto:']"}),
                         [('Alex Example', 'alex@example.test', '')])
