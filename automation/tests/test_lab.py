import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from leads.services import activity
from leads.services.network import FetchError, Response, fetch


class ActivityFeedTests(TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.log = Path(self.folder.name) / "activity.jsonl"
        overrides = override_settings(ACTIVITY_LOG=str(self.log))
        overrides.enable()
        self.addCleanup(overrides.disable)

    def test_entries_carry_the_current_lane_and_round_trip(self):
        with activity.lane("discovery"):
            activity.emit("pick", "Sitemap job https://fictional.example.test/sitemap.xml")
        activity.emit("state", "explicit lane", lane="automation")
        entries = activity.read()
        self.assertEqual([e["lane"] for e in entries], ["discovery", "automation"])
        self.assertEqual(activity.read(after=entries[0]["t"]), entries[1:] if entries[1]["t"] > entries[0]["t"] else [])

    def test_disabled_feed_writes_nothing_and_file_stays_bounded(self):
        with override_settings(ACTIVITY_LOG=""):
            activity.emit("pick", "nothing")
        self.assertFalse(self.log.exists())
        with patch.object(activity, "MAX_BYTES", 2000):
            for index in range(200):
                activity.emit("fetch", f"GET https://fictional.example.test/{index} -> 200")
        self.assertLess(self.log.stat().st_size, 4000)
        self.assertIn("/199 ", activity.read()[-1]["text"])

    def test_fetch_records_status_and_errors_without_page_content(self):
        body = b"<p>Fictional Person fictional.person@example.test</p>"
        with patch("leads.services.network._fetch", return_value=Response("https://fictional.example.test/team", 200, {}, body)):
            with activity.lane("collector"):
                fetch("https://fictional.example.test/team", "UA")
        with patch("leads.services.network._fetch", side_effect=FetchError("HTTP 403; source needs review.", status=403)):
            with self.assertRaises(FetchError):
                fetch("https://fictional.example.test/blocked", "UA")
        ok, error = activity.read()
        self.assertEqual((ok["lane"], ok["kind"], ok["status"], ok["bytes"]), ("collector", "fetch", 200, len(body)))
        self.assertEqual(error["kind"], "error")
        self.assertNotIn("example.test</p>", self.log.read_text())
        self.assertNotIn("fictional.person@", self.log.read_text())

    def test_lab_pages_are_staff_only_and_feed_returns_entries_and_queues(self):
        self.assertEqual(self.client.get("/automation/lab/").status_code, 302)
        self.assertEqual(self.client.get("/automation/lab/feed/").status_code, 302)
        self.client.force_login(get_user_model().objects.create_user("lab-operator", is_staff=True))
        self.assertContains(self.client.get("/automation/lab/"), "Crawler lab")
        activity.emit("result", "https://fictional.example.test/team: 2 contact(s).", lane="collector", contacts=2)
        data = json.loads(self.client.get("/automation/lab/feed/?after=0").content)
        self.assertEqual(data["entries"][-1]["contacts"], 2)
        self.assertEqual(set(data["queues"]), {"collector", "discovery", "automation", "jev"})
        self.assertEqual(data["queues"]["collector"], {"queued": 0, "ready": 0, "next_at": None})
        self.assertEqual(json.loads(self.client.get("/automation/lab/feed/?after=nonsense").content)["entries"][-1]["lane"], "collector")
