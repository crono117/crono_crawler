from django.test import SimpleTestCase, override_settings

from leads.models import Source
from leads.services.extraction import (extract, placeholder_email, placeholder_phone, shared_company_contact,
                                       soup_for, validate_record)

REAL = Source(name="Real-looking", url="https://www.fictional-payments.com/team/", require_sales_role=True)
FIXTURE = Source(name="Fixture", url="https://vendor.example.org/team/", require_sales_role=True)


class PlaceholderContactTests(SimpleTestCase):
    def test_template_framework_and_reserved_emails_are_recognised(self):
        for email, reason in (("logo@2x.png", "file name, not an email"),
                              ("abc123@sentry-next.wixpress.com", "placeholder or framework domain"),
                              ("email@yourdomain.com", "placeholder or framework domain"),
                              ("john.doe@fictional-payments.com", "placeholder mailbox name"),
                              ("yourname@fictional-payments.com", "placeholder mailbox name"),
                              ("alex@example.com", "reserved example domain")):
            with self.subTest(email=email):
                self.assertEqual(placeholder_email(email, REAL), reason)
        # A fixture site on a reserved domain legitimately uses reserved addresses.
        self.assertEqual(placeholder_email("alex@example.com", FIXTURE), "")
        self.assertEqual(placeholder_email("alex.rivera@fictional-payments.com", REAL), "")

    def test_template_phone_numbers_are_recognised(self):
        for phone in ("123-456-7890", "(000) 000-0000", "+1 111 111 1111", "(555) 555-5555", "202-555-0142"):
            with self.subTest(phone=phone):
                self.assertTrue(placeholder_phone(phone, REAL))
        self.assertEqual(placeholder_phone("202-555-0142", FIXTURE), "")
        self.assertEqual(placeholder_phone("(305) 555-1234", REAL), "")
        self.assertEqual(placeholder_phone("(305) 418-2291", REAL), "")

    def test_placeholder_names_and_lorem_ipsum_cards_are_rejected(self):
        for name, evidence in (("John Doe", "John Doe Sales Representative john@fictional-payments.com"),
                               ("Your Name", "Your Name Sales Representative rep@fictional-payments.com"),
                               ("Alex Rivera", "Alex Rivera Sales Representative Lorem ipsum dolor alex@fictional-payments.com")):
            diagnostics = {}
            with self.subTest(name=name):
                self.assertIsNone(validate_record({"name": name, "title": "Sales Representative",
                                                   "email": evidence.split()[-1]}, evidence, REAL, diagnostics))
                self.assertEqual(list(diagnostics["rejected"]), ["placeholder_name"])

    def test_a_card_whose_only_contact_is_a_placeholder_is_rejected_as_such(self):
        diagnostics = {}
        evidence = "Alex Rivera Sales Representative 123-456-7890"
        self.assertIsNone(validate_record({"name": "Alex Rivera", "title": "Sales Representative",
                                           "phone": "123-456-7890"}, evidence, REAL, diagnostics))
        self.assertEqual(diagnostics["rejected"], {"placeholder_contact": 1})

    def test_hidden_honeypot_contacts_are_never_collected(self):
        html = ('<div class="team-member"><h3>Alex Rivera</h3><p class="role">Sales Representative</p>'
                '<span style="display: none"><a href="mailto:trap@fictional-payments.com">trap</a></span></div>'
                '<div class="team-member" hidden><h3>Casey Rivera</h3><p class="role">Sales Representative</p>'
                '<a href="mailto:casey@fictional-payments.com">Email</a></div>')
        self.assertNotIn("trap@", soup_for(html).get_text(" "))
        records, _ = extract(html, REAL)
        self.assertEqual(records, [])

    @override_settings(SHARED_CONTACT_LEADS=True)
    def test_shared_company_contact_skips_placeholders(self):
        html = ('<footer><a href="mailto:info@yourdomain.com">info@yourdomain.com</a> 123-456-7890 '
                '(305) 418-2291</footer>')
        self.assertEqual(shared_company_contact(soup_for(html), REAL), ("phone", "(305) 418-2291", "page header/footer"))
