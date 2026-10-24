from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.cache import cache
from unittest import mock


class LoginRateLimitTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.username = "op1"
        self.password = "CorrectHorseBatteryStaple1!"
        self.user = get_user_model().objects.create_user(self.username, is_staff=True)
        self.user.set_password(self.password)
        self.user.save()
        self.login_url = reverse("login")
        self.ip_a = "203.0.113.10"
        self.ip_b = "198.51.100.22"

    def post_login(self, username, password, ip):
        return self.client.post(
            self.login_url,
            {"username": username, "password": password},
            REMOTE_ADDR=ip,
        )

    def assertLockedMessage(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many failed login attempts")

    def test_lockout_after_five_failures(self):
        # 4 wrong attempts: not yet locked
        for _ in range(4):
            r = self.post_login(self.username, "wrong-pass", self.ip_a)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "Please enter a correct username and password", status_code=200)
        # 5th wrong attempt triggers lock
        r = self.post_login(self.username, "wrong-pass", self.ip_a)
        self.assertLockedMessage(r)
        # 6th attempt (even correct) is refused while locked
        r = self.post_login(self.username, self.password, self.ip_a)
        self.assertLockedMessage(r)

    def test_lock_expires_after_fifteen_minutes(self):
        # Trigger lock
        for _ in range(5):
            self.post_login(self.username, "wrong-pass", self.ip_a)
        # Still locked immediately
        self.assertLockedMessage(self.post_login(self.username, self.password, self.ip_a))
        # Advance time by 15 minutes + 1 second; LocMemCache uses time.time() for expiry
        with mock.patch("time.time") as fake_time:
            base = 1_000_000_000
            fake_time.return_value = base
            # Set the lock at base time (retrigger lock flow)
            cache.clear()
            for _ in range(5):
                self.post_login(self.username, "wrong-pass", self.ip_a)
            # Move past 15 minutes
            fake_time.return_value = base + 15 * 60 + 1
            # Now a correct login should succeed and redirect
            r = self.post_login(self.username, self.password, self.ip_a)
            self.assertIn(r.status_code, (301, 302))
            # Follow-up access should be authenticated
            resp = self.client.get(reverse("leads"))
            self.assertEqual(resp.status_code, 200)

    def test_success_resets_failure_count(self):
        # 4 wrong attempts
        for _ in range(4):
            self.post_login(self.username, "wrong-pass", self.ip_a)
        # Successful login clears counter
        r = self.post_login(self.username, self.password, self.ip_a)
        self.assertIn(r.status_code, (301, 302))
        # Logout to retry (session ends)
        self.client.get(reverse("logout"))
        # Another 4 wrong attempts should not lock yet (count restarted)
        for _ in range(4):
            r = self.post_login(self.username, "wrong-pass", self.ip_a)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "Please enter a correct username and password", status_code=200)
        # 5th wrong now locks again
        r = self.post_login(self.username, "wrong-pass", self.ip_a)
        self.assertLockedMessage(r)

    def test_isolation_by_username_and_ip(self):
        # Lock (username A, IP A)
        for _ in range(5):
            self.post_login(self.username, "wrong-pass", self.ip_a)
        self.assertLockedMessage(self.post_login(self.username, self.password, self.ip_a))
        # Same username from a different IP should not be affected
        r = self.post_login(self.username, self.password, self.ip_b)
        self.assertIn(r.status_code, (301, 302))
        # Different username on same IP should not be affected
        other = get_user_model().objects.create_user("op2", is_staff=True)
        other.set_password(self.password)
        other.save()
        # Log out first to allow a new user session
        self.client.get(reverse("logout"))
        r = self.post_login("op2", self.password, self.ip_a)
        # Verify session is authenticated by accessing a staff-only page
        resp = self.client.get(reverse("leads"))
        self.assertEqual(resp.status_code, 200)

    def test_xff_rotation_does_not_bypass_lock(self):
        # Rotate X-Forwarded-For on each attempt; limiter must still lock by REMOTE_ADDR.
        for i in range(5):
            r = self.client.post(
                self.login_url,
                {"username": self.username, "password": "wrong-pass"},
                REMOTE_ADDR=self.ip_a,
                HTTP_X_FORWARDED_FOR=f"198.51.100.{i+1}",
            )
            self.assertEqual(r.status_code, 200)
        # 6th attempt with correct password must be refused while locked
        r = self.client.post(
            self.login_url,
            {"username": self.username, "password": self.password},
            REMOTE_ADDR=self.ip_a,
            HTTP_X_FORWARDED_FOR="203.0.113.200",
        )
        self.assertLockedMessage(r)

    def test_trusted_proxy_uses_last_xff_entry(self):
        # With TRUST_X_FORWARDED_FOR=1, rotating the left-most spoofed value must not bypass lockout.
        with override_settings(TRUST_X_FORWARDED_FOR=True):
            for i in range(5):
                r = self.client.post(
                    self.login_url,
                    {"username": self.username, "password": "wrong-pass"},
                    REMOTE_ADDR="198.51.100.1",
                    HTTP_X_FORWARDED_FOR=f"203.0.113.{10+i}, {self.ip_a}",
                )
                self.assertEqual(r.status_code, 200)
            r = self.client.post(
                self.login_url,
                {"username": self.username, "password": self.password},
                REMOTE_ADDR="198.51.100.1",
                HTTP_X_FORWARDED_FOR=f"203.0.113.200, {self.ip_a}",
            )
            self.assertLockedMessage(r)


class AdminLoginRateLimitTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.username = "admin1"
        self.password = "CorrectHorseBatteryStaple1!"
        self.user = get_user_model().objects.create_superuser(self.username)
        self.user.set_password(self.password)
        self.user.save()
        self.admin_login_url = reverse("admin:login")
        self.ip = "203.0.113.77"

    def test_admin_login_is_rate_limited(self):
        # 5 wrong attempts to /admin/login/ should lock
        for _ in range(5):
            r = self.client.post(
                self.admin_login_url,
                {"username": self.username, "password": "wrong-pass"},
                REMOTE_ADDR=self.ip,
            )
            self.assertEqual(r.status_code, 200)
        # 6th attempt with correct password must be refused while locked
        r = self.client.post(
            self.admin_login_url,
            {"username": self.username, "password": self.password},
            REMOTE_ADDR=self.ip,
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Too many failed login attempts")

