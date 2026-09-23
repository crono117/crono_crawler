"""Product branding must not imply a ClearPay affiliation."""
import re
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class NeutralBrandingTests(TestCase):
    def assert_neutral(self, response):
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertNotIn("clearpay", re.sub(r"\s+", "", soup.get_text()).casefold())
        return soup

    def test_login_and_console_use_neutral_branding(self):
        soup = self.assert_neutral(self.client.get(reverse("login")))
        self.assertIn("Lead Console", soup.title.get_text())
        operator = get_user_model().objects.create_user("branding-reviewer", is_staff=True)
        self.client.force_login(operator)
        soup = self.assert_neutral(self.client.get(reverse("leads")))
        self.assertEqual(soup.select_one(".brand").get_text(strip=True), "Lead Console")
        self.assertIsNone(soup.select_one(".brand-symbol"))

    def test_admin_login_and_index_use_neutral_branding(self):
        soup = self.assert_neutral(self.client.get(reverse("admin:login")))
        self.assertIn("Lead Console administration", soup.get_text())
        operator = get_user_model().objects.create_superuser("admin-branding-reviewer")
        self.client.force_login(operator)
        soup = self.assert_neutral(self.client.get(reverse("admin:index")))
        self.assertIn("Lead Console", soup.title.get_text())

    def test_export_filename_has_no_company_brand(self):
        operator = get_user_model().objects.create_user("export-branding-reviewer", is_staff=True)
        self.client.force_login(operator)
        response = self.client.get(reverse("export"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="leads.csv"')
