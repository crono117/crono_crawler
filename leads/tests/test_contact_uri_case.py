from django.test import SimpleTestCase, override_settings
from leads.models import Source
from leads.services.extraction import extract
from automation.recipes import evaluate


@override_settings(SHARED_CONTACT_LEADS=False)
class ContactUriCaseTests(SimpleTestCase):
    def test_case_insensitive_schemes_preserve_card_association(self):
        for row, markup in [('div', ''), ('tr', 'table'), ('li', 'ul')]:
            with self.subTest(row=row):
                card = f'''<{row} class="team-member"><h3>Robin Sample</h3><h4>Sales Representative</h4>
                <a href="MAILTO:robin@fixture.example.test?subject=Hello">Email me</a>
                <a href="TeL:+1-202-555-0142">Call me</a></{row}>'''
                html = f'<{markup}>{card}</{markup}>' if markup else card
                source = Source(url='https://fixture.example.test/team')
                records, _ = extract(html, source)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['email'], 'robin@fixture.example.test')
                self.assertEqual(records[0]['phone'], '+1-202-555-0142')
                self.assertIn('robin@fixture.example.test', records[0]['evidence'])
                accepted, _, _ = evaluate(html, source, {})
                self.assertEqual(len(accepted), 1)

    def test_footer_and_neighbor_channel_do_not_attach_to_person(self):
        html = '''<div class="team-member"><h3>Robin Sample</h3><h4>Sales Representative</h4></div>
        <div><a href="MAILTO:robin@fixture.example.test">Email</a></div>
        <footer><a href="TEL:+1-202-555-0142">Office</a></footer>'''
        source = Source(url='https://fixture.example.test/team')
        self.assertEqual(extract(html, source)[0], [])
        self.assertEqual(evaluate(html, source, {})[0], [])

    def test_repeated_sections_do_not_cross_pair_channels(self):
        html = '''<section><div class="team-member"><h3>Robin Sample</h3><h4>Sales Representative</h4>
        <a href="MAILTO:robin@fixture.example.test">Email</a></div></section>
        <section><div class="team-member"><h3>Morgan Sample</h3><h4>Sales Representative</h4>
        <a href="MAILTO:morgan@fixture.example.test">Email</a></div></section>'''
        source = Source(url='https://fixture.example.test/team')
        accepted, _, _ = evaluate(html, source, {})
        self.assertEqual({(r['name'], r['email']) for r in accepted}, {
            ('Robin Sample', 'robin@fixture.example.test'), ('Morgan Sample', 'morgan@fixture.example.test')})
