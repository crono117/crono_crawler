from django.test import TestCase

from leads.models import PageJob, Run, Source
from leads.services.worker import collect_links


class OrdinaryCrawlOrderingTests(TestCase):
    HOME = ('<html><body><a href="/products/terminal/">Card terminal</a>'
            '<a href="/products/gateway/">Payment gateway</a>'
            '<a href="/news/">News</a>'
            '<a href="/team/">Meet our merchant services sales team</a></body></html>')

    def crawl(self, html=HOME, **source):
        values = dict(name='Order fixture', company='Example Payments', url='https://order.example.test/',
                      approved=True, max_pages=3, max_depth=2)
        values.update(source)
        src = Source.objects.create(**values)
        run = Run.objects.create(source=src, status='running')
        job = PageJob.objects.create(run=run, url=src.url)
        collect_links(src, job, html, src.url)
        return list(run.jobs.order_by('id').values_list('url', 'depth'))

    def test_small_allowance_reaches_useful_link_that_appears_last(self):
        jobs = self.crawl()

        self.assertEqual(jobs[0], ('https://order.example.test/', 0))
        self.assertEqual(jobs[1], ('https://order.example.test/team/', 1))
        self.assertEqual(len(jobs), 3)

    def test_low_priority_links_are_ordered_last_but_not_excluded(self):
        urls = [url for url, _ in self.crawl(max_pages=10)]

        self.assertEqual(urls, ['https://order.example.test/', 'https://order.example.test/team/',
                                'https://order.example.test/news/', 'https://order.example.test/products/terminal/',
                                'https://order.example.test/products/gateway/'])

    def test_profile_and_directory_links_outrank_neutral_links(self):
        html = ('<a href="/pricing/">Pricing</a><a href="/locations/">Locations</a>'
                '<a href="/people/alex-example/">Alex Example profile</a><a href="/directory/">Agent directory</a>')
        urls = [url for url, _ in self.crawl(html, max_pages=3)]

        self.assertEqual(urls[1:], ['https://order.example.test/people/alex-example/',
                                    'https://order.example.test/directory/'])

    def test_equal_priority_keeps_document_order(self):
        html = '<a href="/a/">Alpha</a><a href="/b/">Beta</a><a href="/c/">Gamma</a>'
        urls = [url for url, _ in self.crawl(html, max_pages=10)]

        self.assertEqual(urls[1:], ['https://order.example.test/a/', 'https://order.example.test/b/',
                                    'https://order.example.test/c/'])

    def test_out_of_scope_team_link_is_never_queued(self):
        html = '<a href="/pricing/">Pricing</a><a href="/staff/team/">Our sales team</a>'
        urls = [url for url, _ in self.crawl(html, url='https://order.example.test/products/',
                                              allowed_paths='/products/', max_pages=5)]

        self.assertEqual(urls, ['https://order.example.test/products/'])

    def test_campaign_exclusions_are_not_applied_to_ordinary_collection(self):
        html = '<a href="/emv-credit-card-machines/">Machines</a><a href="/blog/">Blog</a>'
        urls = [url for url, _ in self.crawl(html, max_pages=5)]

        self.assertEqual(set(urls[1:]), {'https://order.example.test/emv-credit-card-machines/',
                                         'https://order.example.test/blog/'})

    def test_depth_limit_still_stops_link_collection(self):
        src = Source.objects.create(name='Depth fixture', url='https://order.example.test/', approved=True,
                                    max_pages=5, max_depth=1)
        run = Run.objects.create(source=src, status='running')
        job = PageJob.objects.create(run=run, url='https://order.example.test/team/', depth=1)
        collect_links(src, job, self.HOME, job.url)

        self.assertEqual(run.jobs.count(), 1)
