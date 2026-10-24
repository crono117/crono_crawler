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
    max_candidates = bounded(1000, 10, 10000, help_text="Maximum active saved URLs, including reviewed URLs. Archived and automatically filtered URLs don't count.")
    max_new_domains = bounded(25, 1, 200, help_text="Maximum previously unseen origins per crawl run or independent source search.")
    daily_requests = bounded(200, 1, 5000, help_text="Fetch attempts per UTC day, including robots checks and retries. Redirect hops are separately bounded.")
    min_score = bounded(10, 0, 100)
    interval_hours = bounded(24, 1, 8760)
    search_enabled = models.BooleanField(default=False)
    search_queries = models.TextField(blank=True, max_length=6000, help_text="One exact search query per line, up to 10. Include a location here to target it in search.")
    daily_search_limit = bounded(5, 1, 100)
    auto_search_queries = models.BooleanField(default=False, help_text="Rotate team/contact searches built from campaign industry phrases, sales roles and region, alongside your exact queries.")
    search_precision = models.BooleanField(default=False, help_text="Generated queries ask for pages whose title names a team, staff, people, agents or representatives (intitle:) instead of generic 'contact' pages, so results land on person-listing pages rather than homepages. Exact queries are never rewritten.")
    search_interval_minutes = bounded(60, 15, 1440)
    next_search_at = models.DateTimeField(default=timezone.now)
    search_query_cursor = models.PositiveIntegerField(default=0)
    auto_archive_candidates = models.BooleanField(default=False, help_text="When search needs space, archive eligible dismissed, stale or repeatedly unproductive URLs. Keeps their history and review decisions.")
    max_active_per_origin = bounded(
        0, 0, 10_000,
        help_text="Most active (counted) page URLs one origin may hold; 0 means no per-origin limit. "
                  "Further new pages from that origin are parked outside the active URL limit, so one "
                  "large site cannot fill the inventory. Seeds, sitemaps and company follow-ups are exempt.")
    park_low_value_urls = models.BooleanField(
        default=False,
        help_text="Record newly found in-scope pages without direct team/profile/contact-page intent as parked: "
                  "kept for history and deduplication, never queued, and not counted toward the active URL limit. "
                  "Approval and source bindings are unchanged; a parked URL rediscovered with a direct-intent "
                  "label returns to active inventory when there is room.")
    active = models.BooleanField(default=False)
    next_due_at = models.DateTimeField(default=timezone.now)
    next_intake_at = models.DateTimeField(
        default=timezone.now,
        help_text="Next lightweight pull of fresh collector-discovered links into the active queue, "
                  "independent of the full reseed/sitemap cycle on next_due_at.",
    )
    last_error = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def saved_url_count(self):
        """URLs that count toward max_candidates. Automatically filtered links (dismissed with no
        operator scope) stay on record for analysis but don't use up the campaign's capacity."""
        return self.urls.filter(archived_at__isnull=True).exclude(decision="dismissed", dismissal_scope="").count()

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
    capacity_rejected = models.PositiveIntegerField(default=0)
    parked = models.PositiveIntegerField(default=0)
    intake_only = models.BooleanField(default=False)
    message = models.CharField(max_length=1000, blank=True)

    terminal_reason = models.CharField(max_length=64, blank=True)
    diagnostics = models.JSONField(default=dict)

    def save(self, *args, **kwargs):
        if self.status in ("failed", "paused"):
            self.terminal_reason = self.terminal_reason or "legacy_unknown"
            self.message = self.message or "Run stopped; no historical reason recorded."
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"terminal_reason", "message"}
        return super().save(*args, **kwargs)

    @property
    def needs_recipe_review(self):
        # Job failures are transport or worker errors. A saved run that also had
        # failed jobs is not evidence that the CSS recipe matched nothing.
        return (self.status == "completed" and self.pages_done > 0 and self.contacts_seen == 0
                and self.terminal_reason != "job_failures")

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["campaign"], condition=Q(status__in=["queued", "running", "paused"]), name="one_open_discovery_run"),
            models.CheckConstraint(condition=~Q(status__in=["failed", "paused"]) |
                (~Q(terminal_reason="") & ~Q(message="")), name="discovery_terminal_reason_required")]


class DiscoveredURL(models.Model):
    manual_review_required = models.BooleanField(default=False)
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
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    archive_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-score", "-last_seen"]
        constraints = [models.UniqueConstraint(fields=["campaign", "url"], name="one_url_per_campaign")]


class DiscoveryJob(models.Model):
    company_job = models.ForeignKey("classification.CompanyJob", null=True, blank=True, on_delete=models.PROTECT)
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
    new_contacts = models.PositiveIntegerField(default=0)
    failure_code = models.CharField(max_length=64, blank=True)
    diagnostics = models.JSONField(default=dict)

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


class SourceSearch(models.Model):
    """A source acquisition attempt, independent of crawl run capacity and completion."""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="source_searches")
    run = models.ForeignKey(DiscoveryRun, null=True, blank=True, on_delete=models.SET_NULL)
    query = models.CharField(max_length=600)
    status = models.CharField(max_length=16, default="queued")
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=1000, blank=True)
    summary = models.JSONField(default=dict)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["campaign"],
            condition=Q(status__in=["queued", "processing"]), name="one_pending_source_search")]
