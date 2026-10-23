from django.test import SimpleTestCase, override_settings
from leads.models import Source
from leads.services.extraction import extract


@override_settings(SHARED_CONTACT_LEADS=False)
class MixedCaseContactSchemesTests(SimpleTestCase):
    def test_default_selectors_are_case_insensitive_for_mailto_and_tel(self):
        # Mixed-case MAILTO/Tel targets should be selected by the default selectors.
        html = '''<div class="team-member">
            <h3>Riley Sample</h3><h4>Sales Representative</h4>
            <a href="MaIlTo:riley@fixture.example.test">Email</a>
            <a href="TeL:+1 (202) 555-0142">Call</a>
        </div>'''
        source = Source(url="https://fixture.example.test/team")
        records, _ = extract(html, source)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "riley@fixture.example.test")
        self.assertEqual(records[0]["phone"], "+1 (202) 555-0142")

    def test_lowercase_operator_default_is_upgraded_and_works_with_uppercase_dom(self):
        # Operator-provided default-style selector (lowercase mailto:) is upgraded to case-insensitive default,
        # so it still matches an uppercase DOM MAILTO:/TEL:.
        html = '''<div class="team-member">
            <h3>Alex Sample</h3><h4>Sales Representative</h4>
            <a href="MAILTO:alex@fixture.example.test">Email</a>
            <a href="TEL:+1-202-555-0123">Call</a>
        </div>'''
        source = Source(url="https://fixture.example.test/team", recipe={
            "email": 'a[href^="mailto:"]',
            "phone": 'a[href^="tel:"]',
        })
        records, _ = extract(html, source)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "alex@fixture.example.test")
        self.assertEqual(records[0]["phone"], "+1-202-555-0123")

