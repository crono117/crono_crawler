import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from automation import intake, learning
from automation.models import SiteAssessment, SiteAutomationJob, SitePolicy
from automation.policy import consider, consider_pending
from discovery import acquisition
from discovery.models import Campaign, DiscoveredURL
from discovery.yields import OriginYield
from leads.models import Source
from leads.services.network import origin


def pending(campaign, url, label="", *, method="search", query="", score=0):
    return DiscoveredURL.objects.create(campaign=campaign, url=url, origin=origin(url), label=label, method=method,
                                        search_query=query, decision="pending", kind="page", score=score)


class IntakeCase(TestCase):
    def setUp(self):
        intake._last_pass.clear()
        self.campaign = Campaign.objects.create(name="Intake", active=True, use_sitemaps=False)
        self.policy = SitePolicy.objects.create(campaign=self.campaign, enabled=True, require_page_intent=False,
                                                max_sites_per_day=4, explore_share=50)

    def set_share(self, share, **extra):
        SitePolicy.objects.filter(pk=self.policy.pk).update(explore_share=share, **extra)
        self.policy.refresh_from_db()

    def tiers(self):
        return sorted(SiteAutomationJob.objects.values_list("admission_tier", flat=True))


class TierTests(TestCase):
    def test_tiers_follow_page_intent(self):
        self.assertEqual(intake.tier_for("https://a.example.org/team/"), "direct")
        self.assertEqual(intake.tier_for("https://a.example.org/", "Meet our sales team | Acme"), "direct")
        self.assertEqual(intake.tier_for("https://a.example.org/about-us"), "company")
        self.assertEqual(intake.tier_for("https://a.example.org/contact"), "company")
        self.assertEqual(intake.tier_for("https://a.example.org/"), "generic")
        self.assertEqual(intake.tier_for("https://a.example.org/services"), "generic")

    def test_explore_cap_rounds_up_so_a_nonzero_share_is_never_zero(self):
        policy = SitePolicy(max_sites_per_day=25, explore_share=1)
        self.assertEqual(intake.explore_cap(policy), 1)
        self.assertEqual(intake.explore_cap(SitePolicy(max_sites_per_day=25, explore_share=20)), 5)
        self.assertEqual(intake.explore_cap(SitePolicy(max_sites_per_day=25, explore_share=0)), 0)

    def test_site_family_groups_subdomains_and_handles_two_part_suffixes(self):
        self.assertEqual(learning.site_family("https://www.acme.com/team"), "acme.com")
        self.assertEqual(learning.site_family("https://blog.shop.acme.com"), "acme.com")
        self.assertEqual(learning.site_family("https://x.acme.co.uk/"), "acme.co.uk")
        self.assertEqual(learning.site_family("https://acme.com"), "acme.com")

    def test_shared_hosting_tenants_are_separate_companies(self):
        self.assertEqual(learning.site_family("https://acme.wixsite.com/team"), "acme.wixsite.com")
        self.assertNotEqual(learning.site_family("https://acme.github.io/"), learning.site_family("https://zeta.github.io/"))
        self.assertEqual(learning.site_family("https://shop.acme.myshopify.com/"), "shop.acme.myshopify.com")


class AdmissionTests(IntakeCase):
    def test_legacy_share_keeps_first_eligible_behavior_and_records_why(self):
        self.set_share(100)
        candidate = pending(self.campaign, "https://homepage.example/", query="Q one")
        job = consider(candidate)
        self.assertIsNotNone(job)
        self.assertEqual((job.admission_tier, job.admission_method, job.admission_query), ("generic", "search", "Q one"))
        self.assertEqual(SiteAutomationJob.objects.get(pk=job.pk).admission_reasons,
                         ["First-eligible admission (generic tier)"])

    def test_reserved_mode_defers_exploratory_pages_but_admits_direct_ones(self):
        homepage = pending(self.campaign, "https://homepage.example/")
        team = pending(self.campaign, "https://team.example/team/")
        self.assertIsNone(consider(homepage))
        job = consider(team)
        self.assertEqual(job.admission_tier, "direct")
        self.assertFalse(SiteAutomationJob.objects.filter(source__url=homepage.url).exists())

    def test_allocator_caps_exploratory_slots_and_prefers_higher_tiers(self):
        pending(self.campaign, "https://g1.example/")
        pending(self.campaign, "https://g2.example/")
        pending(self.campaign, "https://g3.example/")
        pending(self.campaign, "https://company.example/about-us")
        pending(self.campaign, "https://direct.example/team/")
        created = intake.admit(self.policy, force=True)
        # max 4/day, share 50% => at most 2 exploratory. The company page outranks generic homepages.
        self.assertEqual(len(created), 3)
        self.assertEqual(self.tiers(), ["company", "direct", "generic"])
        self.assertEqual(SiteAutomationJob.objects.count(), 3)
        again = intake.admit(self.policy, force=True)
        self.assertEqual(again, [])

    def test_zero_share_admits_only_direct_pages(self):
        self.set_share(0)
        pending(self.campaign, "https://g1.example/")
        pending(self.campaign, "https://company.example/about-us")
        pending(self.campaign, "https://direct.example/team/")
        intake.admit(self.policy, force=True)
        self.assertEqual(self.tiers(), ["direct"])

    def test_a_sites_best_pending_page_is_its_setup_start(self):
        pending(self.campaign, "https://acme.example/")
        pending(self.campaign, "https://acme.example/about-us")
        created = intake.admit(self.policy, force=True)
        self.assertEqual([job.source.url for job in created], ["https://acme.example/about-us"])
        self.assertEqual(created[0].admission_tier, "company")

    def test_one_exploratory_site_per_company_family_per_day(self):
        self.set_share(90, max_sites_per_day=10)
        pending(self.campaign, "https://www.acme.com/")
        pending(self.campaign, "https://shop.acme.com/")
        pending(self.campaign, "https://other.example/")
        intake.admit(self.policy, force=True)
        families = sorted(learning.site_family(job.source.url) for job in SiteAutomationJob.objects.all())
        self.assertEqual(families, ["acme.com", "other.example"])

    def test_tenants_of_a_shared_platform_do_not_count_as_one_company(self):
        self.set_share(90, max_sites_per_day=10)
        pending(self.campaign, "https://acme.wixsite.com/")
        pending(self.campaign, "https://zeta.wixsite.com/")
        intake.admit(self.policy, force=True)
        self.assertEqual(SiteAutomationJob.objects.count(), 2)

    def test_direct_pages_are_not_limited_by_the_family_rule(self):
        self.set_share(50, max_sites_per_day=10)
        pending(self.campaign, "https://sales.acme.com/team/")
        pending(self.campaign, "https://agents.acme.com/agents/")
        intake.admit(self.policy, force=True)
        self.assertEqual(SiteAutomationJob.objects.count(), 2)

    def test_daily_cap_still_applies_in_ranked_mode(self):
        self.set_share(100, max_sites_per_day=4)
        self.set_share(60)
        for index in range(6):
            pending(self.campaign, f"https://d{index}.example/team/")
        intake.admit(self.policy, force=True)
        self.assertEqual(SiteAutomationJob.objects.count(), 4)

    def test_candidates_the_gate_rejects_never_hide_a_sites_admissible_page(self):
        pending(self.campaign, "https://acme.example/team/?page=2")
        pending(self.campaign, "https://acme.example/")
        created = intake.admit(self.policy, force=True)
        self.assertEqual([job.source.url for job in created], ["https://acme.example/"])

    def test_inadmissible_pages_cannot_starve_the_bounded_ranked_window(self):
        from unittest.mock import patch
        self.set_share(100)
        self.set_share(50, max_sites_per_day=1)
        # Many higher-ranked pending pages whose sites already have a source, or whose path is out of scope.
        for index in range(12):
            Source.objects.create(name=f"s{index}", url=f"https://has-source{index}.example/", approved=True)
            pending(self.campaign, f"https://has-source{index}.example/team/", score=99)
            pending(self.campaign, f"https://out-of-scope{index}.example/services/deep", score=98)
        Campaign.objects.filter(pk=self.campaign.pk).update(max_pages=10)
        pending(self.campaign, "https://admissible.example/")
        with patch.object(intake, "MAX_CONSIDERED", 3):
            created = intake.admit(self.policy, force=True)
        self.assertEqual([job.source.url for job in created], ["https://admissible.example/"])

    def test_dismissed_origin_sites_are_filtered_out_in_bulk(self):
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://nope.example/", origin="https://nope.example",
                                     decision="dismissed", dismissal_scope="origin", kind="page")
        pending(self.campaign, "https://www.nope.example/team/")
        self.assertEqual(intake.ranked_sites(self.policy), [])

    def test_excluded_and_denied_pages_are_not_admitted(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(exclusions="path:careers")
        self.campaign.refresh_from_db()
        pending(self.campaign, "https://jobs.example.org/careers/team")
        pending(self.campaign, "https://linkedin.com/team/")
        self.assertEqual(intake.admit(self.policy, force=True), [])

    def test_consider_pending_routes_through_the_allocator_when_share_is_reduced(self):
        pending(self.campaign, "https://homepage.example/")
        pending(self.campaign, "https://direct.example/team/")
        consider_pending()
        self.assertEqual(self.tiers(), ["direct", "generic"])

    def test_allocator_is_throttled_between_ticks(self):
        pending(self.campaign, "https://one.example/")
        self.assertEqual(len(intake.admit(self.policy)), 1)
        pending(self.campaign, "https://two.example/")
        self.assertEqual(intake.admit(self.policy), [])
        self.assertEqual(len(intake.admit(self.policy, force=True)), 1)


class LearningTests(IntakeCase):
    def job(self, url, outcome, *, query="", state="paused"):
        source = Source.objects.create(name=url, url=url, approved=True, setup_mode="automatic", active=False)
        job = SiteAutomationJob.objects.create(source=source, campaign=self.campaign, scope_hash="x", state=state,
                                               admission_query=query, admission_tier="generic", admission_method="search")
        # Earlier days' history: it must not count against today's slots.
        SiteAutomationJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(days=2))
        if outcome:
            SiteAssessment.objects.create(job=job, source=source, origin=origin(url), outcome=outcome)
        return job

    def test_failed_company_family_ranks_below_a_fresh_site(self):
        self.set_share(50, max_sites_per_day=1)
        self.job("https://old.bad-co.example/", "blocked")
        pending(self.campaign, "https://shop.bad-co.example/")
        pending(self.campaign, "https://fresh.example/")
        created = intake.admit(self.policy, force=True)
        self.assertEqual([job.source.url for job in created], ["https://fresh.example/"])

    def test_family_that_produced_contacts_gets_a_bonus_over_a_neutral_site(self):
        self.set_share(50, max_sites_per_day=1)
        self.job("https://old.good-co.example/", "contacts_found", state="active")
        pending(self.campaign, "https://neutral.example/")
        pending(self.campaign, "https://shop.good-co.example/")
        created = intake.admit(self.policy, force=True)
        self.assertEqual([job.source.url for job in created], ["https://shop.good-co.example/"])

    def test_query_stats_count_each_site_once_and_ignore_mechanical_failures(self):
        self.job("https://a.example.org/", "no_people_listed", query="Q")
        self.job("https://b.example.org/", "blocked", query="Q")
        self.job("https://c.example.org/", "contacts_found", query="Q", state="active")
        self.job("https://d.example.org/", "redirect_out_of_scope", query="Q")
        self.job("https://e.example.org/", "unavailable", query="Q")
        self.job("https://f.example.org/", "", query="Q", state="probing")
        self.job("https://g.example.org/", "no_people_listed", query="Other")
        stats = learning.query_setup_stats({"Q", "Other", "Missing"})
        self.assertEqual(stats["Q"], OriginYield(fetched=3, productive=1))
        self.assertEqual(stats["Other"], OriginYield(fetched=1, productive=0))
        self.assertNotIn("Missing", stats)

    def test_query_adjustment_needs_evidence_and_is_bounded(self):
        self.assertEqual(learning.query_adjustment(OriginYield(1, 0)), (0, ""))
        bad, reason = learning.query_adjustment(OriginYield(10, 0))
        self.assertLess(bad, 0)
        self.assertGreaterEqual(bad, -learning.QUERY_SPAN)
        self.assertIn("Query admitted 10 sites", reason)
        good, _ = learning.query_adjustment(OriginYield(10, 10))
        self.assertGreater(good, 0)
        self.assertLessEqual(good, learning.QUERY_SPAN)

    def test_a_query_with_only_durable_misses_ranks_its_results_lower(self):
        self.set_share(50, max_sites_per_day=1)
        for index in range(4):
            self.job(f"https://miss{index}.example.org/", "no_people_listed", query="Bad query")
        pending(self.campaign, "https://from-bad.example/", query="Bad query")
        pending(self.campaign, "https://from-new.example/", query="New query")
        created = intake.admit(self.policy, force=True)
        self.assertEqual([job.source.url for job in created], ["https://from-new.example/"])

    def test_merge_yields_adds_counts(self):
        merged = learning.merge_yields({"a": OriginYield(2, 1)}, {"a": OriginYield(3, 0), "b": OriginYield(1, 1)})
        self.assertEqual(merged, {"a": OriginYield(5, 1), "b": OriginYield(1, 1)})

    def test_admission_funnel_groups_outcomes_by_tier_and_query(self):
        self.job("https://a.example.org/", "no_people_listed", query="Q")
        self.job("https://b.example.org/", "contacts_found", query="Q", state="active")
        funnel = learning.admission_funnel(self.campaign)
        row = funnel["by_tier_and_method"][0]
        self.assertEqual((row["key"], row["jobs"], row["active"]), ("generic/search", 2, 1))
        self.assertEqual(row["outcomes"], {"contacts_found": 1, "no_people_listed": 1})
        self.assertEqual(funnel["by_query"][0]["key"], "Q")

    def test_schedule_uses_setup_outcomes_when_choosing_a_query(self):
        from unittest.mock import patch
        Campaign.objects.filter(pk=self.campaign.pk).update(search_enabled=True, search_queries="good query\nbad query")
        self.campaign.refresh_from_db()
        for index in range(6):
            self.job(f"https://good{index}.example.org/", "contacts_found", query="good query", state="active")
            self.job(f"https://bad{index}.example.org/", "no_people_listed", query="bad query")
        seen = {}
        original = acquisition.search_priority if hasattr(acquisition, "search_priority") else None
        from discovery import yields
        def deterministic(stats, rng=None):
            seen[len(seen)] = stats
            return stats.productive - (stats.fetched - stats.productive)
        with patch("discovery.acquisition.search_ready", return_value=True), \
                patch("discovery.acquisition.has_room", return_value=True), \
                patch.object(yields, "search_priority", deterministic):
            attempt = acquisition.schedule(self.campaign)
        self.assertEqual(attempt.query, "good query")
        self.assertEqual({stats.productive for stats in seen.values()}, {0, 6})


class PrecisionQueryTests(TestCase):
    def campaign(self, **extra):
        return Campaign.objects.create(name="Queries", keywords="POS reseller\nmerchant services", sales_terms="sales agent",
                                       region="California", auto_search_queries=True, search_queries='"exact" "query"', **extra)

    def test_legacy_pool_is_unchanged(self):
        pool = acquisition.query_pool(self.campaign())
        self.assertIn('"POS reseller" "contact" "California"', pool)
        self.assertEqual(pool[0], '"exact" "query"')
        self.assertFalse(any("intitle:" in query for query in pool))

    def test_precision_pool_targets_team_titles_and_drops_bare_contact(self):
        pool = acquisition.query_pool(self.campaign(search_precision=True))
        self.assertEqual(pool[0], '"exact" "query"')
        self.assertIn('"POS reseller" intitle:team "California"', pool)
        self.assertIn('"merchant services" intitle:representatives "California"', pool)
        self.assertFalse(any('"contact"' in query for query in pool))
        self.assertEqual(len(pool), len(set(pool)))
        self.assertTrue(all(len(query) <= 600 and len(query.split()) <= 75 for query in pool))

    def test_precision_titles_all_name_direct_intent_pages(self):
        for word in acquisition.PRECISION_TITLES:
            self.assertEqual(intake.tier_for("https://x.example.org/", f"Our {word} | Acme"), "direct", word)

    def test_precision_without_generated_queries_changes_nothing(self):
        campaign = Campaign.objects.create(name="Exact only", keywords="POS", search_queries='"exact"', search_precision=True)
        self.assertEqual(acquisition.query_pool(campaign), ['"exact"'])


class CommandTests(IntakeCase):
    def run_command(self, name, *args):
        out = StringIO()
        call_command(name, *args, stdout=out)
        return json.loads(out.getvalue())

    def test_policy_command_sets_explore_share_without_pausing_jobs(self):
        job = consider(pending(self.campaign, "https://direct.example/team/"))
        dry = self.run_command("automation_policy", "--campaign", str(self.campaign.pk), "--explore-share", "20")
        self.assertFalse(dry["applied"])
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.explore_share, 50)
        done = self.run_command("automation_policy", "--campaign", str(self.campaign.pk), "--explore-share", "20", "--apply")
        self.assertTrue(done["applied"])
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.explore_share, 20)
        job.refresh_from_db()
        self.assertEqual(job.state, "probe_queued")

    def test_policy_command_rejects_out_of_range_share(self):
        with self.assertRaises(CommandError):
            self.run_command("automation_policy", "--campaign", str(self.campaign.pk), "--explore-share", "101")

    def test_preview_is_read_only_and_matches_what_the_allocator_then_does(self):
        pending(self.campaign, "https://g1.example/")
        pending(self.campaign, "https://company.example/about-us")
        pending(self.campaign, "https://direct.example/team/")
        before = (SiteAutomationJob.objects.count(), Source.objects.count())
        result = self.run_command("automation_intake", "--campaign", str(self.campaign.pk))
        self.assertEqual((SiteAutomationJob.objects.count(), Source.objects.count()), before)
        preview = result["preview"]
        self.assertEqual(preview["admissible_sites_by_tier"], {"direct": 1, "company": 1, "generic": 1})
        self.assertEqual(preview["slots_remaining"], 4)
        self.assertEqual(preview["explore_cap"], 2)
        planned = [row["url"] for row in preview["would_admit"]]
        created = [job.source.url for job in intake.admit(self.policy, force=True)]
        self.assertEqual(sorted(planned), sorted(created))
        self.assertEqual(preview["would_admit"][0]["tier"], "direct")
        self.assertTrue(preview["would_admit"][0]["reasons"])

    def test_preview_can_try_a_proposed_share(self):
        for index in range(3):
            pending(self.campaign, f"https://g{index}.example/")
        zero = self.run_command("automation_intake", "--campaign", str(self.campaign.pk), "--explore-share", "0")
        self.assertEqual(zero["preview"]["would_admit"], [])
        self.assertEqual(zero["preview"]["not_admissible"]["explore_cap"], 3)
        self.assertEqual(self.run_command("automation_intake", "--campaign", str(self.campaign.pk))["saved_explore_share"], 50)
        with self.assertRaises(CommandError):
            self.run_command("automation_intake", "--campaign", str(self.campaign.pk), "--explore-share", "-1")

    def test_audit_reports_intake_tiers_and_funnel(self):
        pending(self.campaign, "https://g1.example/")
        pending(self.campaign, "https://direct.example/team/")
        result = self.run_command("automation_audit", "--campaign", str(self.campaign.pk), "--no-jobs")
        pool = result["summary"]["pending_pool"]
        self.assertEqual(pool["admissible_sites_by_intake_tier"], {"direct": 1, "generic": 1})
        self.assertIn("by_tier_and_method", result["summary"]["admission_funnel"])
