from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from leads.models import CATEGORIES


def bounded(default, low, high, **kwargs):
    return models.PositiveIntegerField(default=default, validators=[MinValueValidator(low), MaxValueValidator(high)], **kwargs)


class Campaign(models.Model):
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=40, choices=CATEGORIES, default="merchant_services")
    sources = models.ManyToManyField("leads.Source", related_name="discovery_campaigns", blank=True)
    keywords = models.TextField(default="merchant services\npayment processing\npoint of sale\nPOS reseller", max_length=3000)
    sales_terms = models.TextField(default="sales\nrepresentative\nagent\nreseller\nconsultant\npartner", max_length=3000)
    exclusions = models.TextField(blank=True, max_length=3000)
    region = models.CharField(max_length=160, blank=True)
    use_sitemaps = models.BooleanField(default=True)
    max_pages = bounded(50, 1, 500, help_text="Maximum page and sitemap jobs in each run.")
    max_depth = bounded(3, 0, 5)
    max_candidates = bounded(1000, 10, 10000, help_text="Maximum saved URLs in this campaign, including reviewed URLs.")
    max_new_domains = bounded(25, 1, 200, help_text="Maximum previously unseen origins discovered in each run.")
    daily_requests = bounded(200, 1, 5000, help_text="Fetch attempts per UTC day, including robots checks and retries. Redirect hops are separately bounded.")
    min_score = bounded(10, 0, 100)
    interval_hours = bounded(24, 1, 8760)
    search_enabled = models.BooleanField(default=False)
    search_queries = models.TextField(blank=True, max_length=6000, help_text="One exact search query per line, up to 10. Include a location here to target it in search.")
    daily_search_limit = bounded(5, 1, 100)
    active = models.BooleanField(default=False)
    next_due_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DiscoveryRun(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, default="queued")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    last_tick_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    pages_done = models.PositiveIntegerField(default=0)
    contacts_seen = models.PositiveIntegerField(default=0)
    new_contacts = models.PositiveIntegerField(default=0)
    candidates_found = models.PositiveIntegerField(default=0)
    new_domains = models.PositiveIntegerField(default=0)
    duplicates_seen = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=1000, blank=True)

    @property
    def needs_recipe_review(self):
        return self.status == "completed" and self.pages_done > 0 and self.contacts_seen == 0

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["campaign"], condition=Q(status__in=["queued", "running", "paused"]), name="one_open_discovery_run")]


class DiscoveredURL(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="urls")
    first_run = models.ForeignKey(DiscoveryRun, on_delete=models.SET_NULL, null=True, related_name="found_urls")
    url = models.URLField(max_length=1500)
    origin = models.CharField(max_length=500, db_index=True)
    label = models.CharField(max_length=200, blank=True)
    context = models.CharField(max_length=600, blank=True)
    found_on = models.URLField(max_length=1500, blank=True)
    method = models.CharField(max_length=20, default="link")
    kind = models.CharField(max_length=10, default="page", choices=[("page", "Page"), ("sitemap", "Sitemap")])
    search_query = models.CharField(max_length=600, blank=True)
    score = models.IntegerField(default=0)
    reasons = models.JSONField(default=list)
    decision = models.CharField(max_length=16, default="pending", choices=[("pending", "Needs review"), ("approved", "Approved scope"), ("dismissed", "Dismissed")])
    dismissal_scope = models.CharField(max_length=8, blank=True, default="", choices=[("", "Automatic filter / none"), ("url", "Operator URL dismissal"), ("origin", "Operator origin dismissal")])
    source = models.ForeignKey("leads.Source", on_delete=models.SET_NULL, null=True, blank=True, related_name="discovered_urls")
    last_result = models.CharField(max_length=1000, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-score", "-last_seen"]
        constraints = [models.UniqueConstraint(fields=["campaign", "url"], name="one_url_per_campaign")]


class DiscoveryJob(models.Model):
    run = models.ForeignKey(DiscoveryRun, on_delete=models.CASCADE, related_name="jobs")
    candidate = models.ForeignKey(DiscoveredURL, on_delete=models.CASCADE, null=True, blank=True, related_name="jobs")
    source = models.ForeignKey("leads.Source", on_delete=models.SET_NULL, null=True, blank=True)
    kind = models.CharField(max_length=10, default="page", choices=[("page", "Page"), ("sitemap", "Sitemap"), ("search", "Search")])
    url = models.CharField(max_length=1500)
    depth = models.PositiveIntegerField(default=0)
    priority = models.IntegerField(default=0)
    status = models.CharField(max_length=16, default="queued")
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=1000, blank=True)
    contacts_seen = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-priority", "id"]
        constraints = [models.UniqueConstraint(fields=["run", "kind", "url"], name="one_discovery_job")]


class DailyUsage(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="usage")
    day = models.DateField()
    requests = models.PositiveIntegerField(default=0)
    searches = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["campaign", "day"], name="one_discovery_daily_usage")]
