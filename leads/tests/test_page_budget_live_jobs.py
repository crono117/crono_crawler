from django.test import TestCase

from leads.models import PageJob, Run, Source
from leads.services.worker import collect_links


class PageBudgetLiveJobsTests(TestCase):
    def make_source_and_run(self, **overrides):
        values = dict(
            name="Budget fixture",
            company="Example Co",
            url="https://budget.example.test/",
            approved=True,
            max_pages=3,
            max_depth=2,
        )
        values.update(overrides)
        src = Source.objects.create(**values)
        run = Run.objects.create(source=src, status="running")
        job = PageJob.objects.create(run=run, url=src.url)
        return src, run, job

    def test_failed_and_skipped_do_not_consume_budget(self):
        src, run, job = self.make_source_and_run(max_pages=3)
        # Existing non-live jobs should not count toward the remaining page budget.
        PageJob.objects.create(run=run, url="https://budget.example.test/old-failed/", status="failed")
        PageJob.objects.create(run=run, url="https://budget.example.test/old-skipped/", status="skipped")

        html = (
            '<a href="/a/">A</a>'
            '<a href="/b/">B</a>'
            '<a href="/c/">C</a>'
            '<a href="/d/">D</a>'
            '<a href="/team/">Team</a>'
        )

        collect_links(src, job, html, src.url)

        # Only live jobs (done/processing/queued) should count toward the allowance,
        # and we should enqueue new links until max_pages live pages is reached.
        live_statuses = ("done", "processing", "queued")
        live_count = run.jobs.filter(status__in=live_statuses).count()
        self.assertEqual(live_count, src.max_pages)

        # Ensure we actually created exactly two follow-ups (3 live total minus the root job).
        followups = list(
            run.jobs.exclude(url=src.url).filter(status__in=live_statuses).values_list("url", flat=True)
        )
        self.assertEqual(len(followups), 2)
        # New follow-ups should come from the provided HTML links.
        self.assertTrue(
            all(url.startswith("https://budget.example.test/") for url in followups)
        )

    def test_never_exceeds_max_pages_when_already_at_limit(self):
        src, run, job = self.make_source_and_run(max_pages=3)
        # Already at the live page limit: the root job plus two queued follow-ups.
        PageJob.objects.create(run=run, url="https://budget.example.test/alpha/", status="queued", depth=1)
        PageJob.objects.create(run=run, url="https://budget.example.test/beta/", status="queued", depth=1)

        before_urls = list(run.jobs.values_list("url", flat=True))

        html = '<a href="/gamma/">Gamma</a><a href="/delta/">Delta</a>'
        collect_links(src, job, html, src.url)

        # No new jobs should be created because we are already at max_pages live jobs.
        after_urls = list(run.jobs.values_list("url", flat=True))
        self.assertEqual(before_urls, after_urls)

