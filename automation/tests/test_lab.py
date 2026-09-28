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
        self.assertEqual(set(data["queues"]), {"discovery", "approval", "automation", "collector", "jev"})
        self.assertEqual(data["queues"]["collector"], {"queued": 0, "ready": 0, "next_at": None})
        self.assertEqual(data["review"], {"worth_a_look": 0, "low_signal": 0, "auto_filtered": 0})
        self.assertEqual(data["leads"], {"total": 0, "today": 0, "shared": 0})
        self.assertEqual(data["recent_leads"], [])
        self.assertEqual(json.loads(self.client.get("/automation/lab/feed/?after=nonsense").content)["entries"][-1]["lane"], "collector")


from automation.tests.test_pipeline import AutomationCase


class ApprovalActivityTests(AutomationCase):
    def setUp(self):
        super().setUp()
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        overrides = override_settings(ACTIVITY_LOG=str(Path(folder.name) / "activity.jsonl"))
        overrides.enable()
        self.addCleanup(overrides.disable)

    def test_policy_approvals_filters_and_operator_dismissals_reach_the_approval_lane(self):
        from discovery.models import DiscoveredURL
        from discovery.services import register
        self.candidate()  # policy-approvable team page
        register(self.run, "https://drive.google.com/file/d/fictional/view", label="Sales deck")
        pending = register(self.run, "https://unscored.example.org/", label="")
        self.client.force_login(get_user_model().objects.create_user("approval-operator", is_staff=True))
        self.client.post(f"/discovery/candidates/{pending.pk}/", {"action": "dismiss"})
        approval = [e for e in activity.read() if e["lane"] == "approval"]
        kinds = [e["kind"] for e in approval]
        self.assertIn("approved", kinds)
        self.assertEqual(next(e for e in approval if e["kind"] == "approved")["by"], "policy")
        self.assertIn("filtered", kinds)
        self.assertIn("pending", kinds)
        self.assertEqual(DiscoveredURL.objects.get(pk=pending.pk).decision, "dismissed")
        self.assertEqual(approval[-1]["kind"], "dismissed")
        self.assertEqual(approval[-1]["by"], "operator")


class LeadEventTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        overrides = override_settings(ACTIVITY_LOG=str(Path(folder.name) / "activity.jsonl"))
        overrides.enable()
        self.addCleanup(overrides.disable)

    def test_saved_leads_are_announced_after_commit_with_counts_only(self):
        from leads.models import Source
        from leads.services.storage import save_records
        source = Source.objects.create(name="Fictional", url="https://vendor.example.org/team/", approved=True)
        record = {"name": "Alex Fictional", "company": "", "title": "Sales Representative",
                  "email": "info@vendor.example.org", "phone": "", "contact_scope": "shared",
                  "person_tags": [], "evidence": "Alex Fictional Sales Representative info@vendor.example.org"}
        with self.captureOnCommitCallbacks(execute=True):
            with activity.lane("automation"):
                save_records(source, source.url, "hash", [record], [])
        (entry,) = [e for e in activity.read() if e["kind"] == "leads"]
        self.assertEqual((entry["lane"], entry["contacts"], entry["seen"]), ("automation", 1, 1))
        self.assertIn("1 with shared company contact", entry["text"])
        self.assertNotIn("Alex", json.dumps(entry))
        self.assertNotIn("info@", entry["text"])

    def test_fresh_lab_shows_each_lanes_recent_history(self):
        for index in range(200):
            activity.emit("pick", f"discovery {index}", lane="discovery")
        activity.emit("state", "setup moved", lane="automation")
        for index in range(200):
            activity.emit("pick", f"discovery later {index}", lane="discovery")
        recent = activity.recent_by_lane(per_lane=40)
        self.assertIn("setup moved", [e["text"] for e in recent])
        self.assertEqual(sum(e["lane"] == "discovery" for e in recent), 40)

    def test_lead_panel_lists_real_leads_newest_first_and_skips_demo_fixtures(self):
        from leads.models import Lead, Observation, Source
        from automation.views import recent_leads
        real = Source.objects.create(name="Real", url="https://vendor.example.org/team/", approved=True)
        demo = Source.objects.create(name="Demo", url="https://demo.example.org/", approved=True, collector="demo")
        for index, source in enumerate((real, demo)):
            lead = Lead.objects.create(identity=f"id-{index}", name=f"Fictional Person{index}", title="Sales Representative",
                                       email="info@vendor.example.org", contact_scope="shared")
            Observation.objects.create(lead=lead, source=source, page_url=source.url, evidence="x")
        rows = recent_leads(Lead)
        self.assertEqual([r["name"] for r in rows], ["Fictional Person0"])
        self.assertEqual((rows[0]["site"], rows[0]["shared"]), ("vendor.example.org", True))
