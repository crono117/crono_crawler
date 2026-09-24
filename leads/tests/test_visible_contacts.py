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
