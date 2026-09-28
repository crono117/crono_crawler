from django.test import SimpleTestCase, override_settings

from automation.recipes import evaluate, proposals
from leads.models import Source
from leads.services.extraction import extract


def page(cards, footer='<footer>Call (555) 010-2000 · <a href="mailto:info@fictional.example.org">info@fictional.example.org</a></footer>'):
    body = "".join(f'<div class="team-member"><h3>{name}</h3><p class="role">{title}</p>{extra}</div>'
                   for name, title, extra in cards)
    return f"<html><body><main>{body}</main>{footer}</body></html>"


CARDS = [("Alex Fictional", "Merchant Services Sales Consultant", ""),
         ("Casey Example", "Office Manager", ""),
         ("Jordan Sample", "Sales Representative", '<a href="mailto:jordan@fictional.example.org">Email</a>')]


class SharedContactLeadTests(SimpleTestCase):
    def setUp(self):
        self.source = Source(name="Fictional", url="https://www.fictional.example.org/team/", company="",
                             require_sales_role=True)

    def recipe(self, html):
        return next(r for r in proposals([html]) if r.get("row") == ".team-member")

    def test_off_by_default(self):
        html = page(CARDS)
        records, _, stats = evaluate(html, self.source, self.recipe(html))
        self.assertEqual([r["name"] for r in records], ["Jordan Sample"])
        self.assertEqual(stats["shared_contact_records"], 0)

    @override_settings(SHARED_CONTACT_LEADS=True)
    def test_named_salesperson_gets_the_sites_shared_inbox_labelled_as_shared(self):
        html = page(CARDS)
        records, _, stats = evaluate(html, self.source, self.recipe(html))
        by_name = {r["name"]: r for r in records}
        self.assertEqual(set(by_name), {"Alex Fictional", "Jordan Sample"})  # office manager is not sales
        alex = by_name["Alex Fictional"]
        self.assertEqual((alex["email"], alex["contact_scope"]), ("info@fictional.example.org", "shared"))
        self.assertIn("Shared company contact shown on this page (company inbox on this page), not this person's own",
                      alex["evidence"])
        # A personal contact on the card always wins and stays a personal lead.
        self.assertEqual(by_name["Jordan Sample"]["email"], "jordan@fictional.example.org")
        self.assertNotEqual(by_name["Jordan Sample"]["contact_scope"], "shared")
        self.assertEqual(stats["shared_contact_records"], 1)

    @override_settings(SHARED_CONTACT_LEADS=True)
    def test_third_party_inbox_is_never_used_but_a_footer_phone_is(self):
        footer = '<footer>Built by <a href="mailto:info@webhost.example.net">info@webhost.example.net</a> · (555) 010-2000</footer>'
        html = page(CARDS[:1], footer=footer)
        records, _, _ = evaluate(html, self.source, self.recipe(html))
        self.assertEqual([(r["email"], r["phone"], r["contact_scope"]) for r in records],
                         [("", "(555) 010-2000", "shared")])
        html = page(CARDS[:1], footer="<footer>Built by info@webhost.example.net</footer>")
        self.assertEqual(evaluate(html, self.source, self.recipe(html))[0], [])

    @override_settings(SHARED_CONTACT_LEADS=True)
    def test_operator_rules_sources_use_the_same_rule(self):
        records, _ = extract(page(CARDS), self.source)
        shared = [r for r in records if r["contact_scope"] == "shared"]
        self.assertEqual([(r["name"], r["email"]) for r in shared], [("Alex Fictional", "info@fictional.example.org")])


class DecoyAssessmentTests(SimpleTestCase):
    def test_placeholder_rejections_and_protected_emails_become_assessment_reasons(self):
        from automation.assessments import build
        source = Source(name="Fictional", url="https://www.fictional-payments.com/team/")
        url = "https://www.fictional-payments.com/team/"
        stats = {url: {"row": ".team-member", "matched_cards": 3, "accepted_records": 0,
                       "primary_rejections": {"placeholder_contact": 2, "placeholder_name": 1},
                       "signals": {"name": 3, "titled": 3, "role": 3, "contact": 3}}}
        meta = {url: {"contact_mechanisms": {"mailto": 0, "tel": 0, "protected_emails": 4}}}
        _, reasons, options, facts, _ = build(source, stats, meta)
        self.assertIn("placeholder_or_decoy_contacts", reasons)
        self.assertIn("emails_hidden_by_protection", reasons)
        self.assertEqual(facts["protected_emails"], 4)
