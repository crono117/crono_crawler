from django.db import models
from django.db.models import Q
from django.utils import timezone

CATEGORIES = [
    ("merchant_services", "Merchant services"), ("pos", "Point of sale"), ("payroll", "Payroll & HR"),
    ("funding", "Business funding"), ("telecom", "Business telecom"), ("it_services", "IT & managed services"),
    ("insurance", "Business insurance"), ("other", "Other / unclassified"),
]

class Source(models.Model):
    name = models.CharField(max_length=160)
    url = models.URLField(max_length=1500, unique=True)
    company = models.CharField(max_length=160, blank=True)
    category = models.CharField(max_length=40, choices=CATEGORIES, default="merchant_services")
    active = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)
    approval_notes = models.TextField(blank=True)
    collector = models.CharField(max_length=12, choices=[("http", "HTML"), ("browser", "Browser"), ("demo", "Demo")], default="http")
    extractor = models.CharField(max_length=12, choices=[("rules", "CSS / structured HTML"), ("ollama", "Local Ollama")], default="rules")
    require_sales_role = models.BooleanField(default=True)
    recipe = models.JSONField(default=dict, blank=True)
    setup_mode = models.CharField(max_length=16, default="rules_only", choices=[("rules_only", "Operator-configured rules"), ("automatic", "Automated recipe setup")])
    approval_kind = models.CharField(max_length=12, default="operator", choices=[("operator", "Operator review"), ("policy", "Campaign policy")])
    allow_homepage = models.BooleanField(default=False, help_text="Allow the exact homepage in addition to the path prefixes; does not allow the whole origin.")
    allowed_paths = models.TextField(default="/", help_text="One URL path prefix per line. / allows the whole site.")
    follow_links = models.BooleanField(default=True)
    discover_external = models.BooleanField(default=False)
    interval_hours = models.PositiveIntegerField(default=168)
    delay_seconds = models.PositiveIntegerField(default=5)
    max_pages = models.PositiveIntegerField(default=25)
    max_depth = models.PositiveIntegerField(default=2)
    next_due_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
    def __str__(self):
        return self.name

class Run(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, default="queued", choices=[(x, x.title()) for x in ("queued", "running", "completed", "paused", "failed", "cancelled")])
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    pages_done = models.PositiveIntegerField(default=0)
    contacts_seen = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True)

    @property
    def needs_recipe_review(self):
        return self.status == "completed" and self.pages_done > 0 and self.contacts_seen == 0

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["source"], condition=Q(status__in=["queued", "running", "paused"]), name="one_open_run_per_source")]

class PageJob(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="jobs")
    url = models.URLField(max_length=1500)
    depth = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, default="queued")
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    message = models.TextField(blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "url"], name="one_url_per_run")]
        ordering = ["id"]

class DomainState(models.Model):
    origin = models.CharField(max_length=500, unique=True)
    next_allowed_at = models.DateTimeField(default=timezone.now)
    robots_text = models.TextField(blank=True)
    robots_checked_at = models.DateTimeField(null=True)

class WorkerLease(models.Model):
    key = models.CharField(max_length=40, primary_key=True)
    token = models.CharField(max_length=64, blank=True)
    heartbeat_at = models.DateTimeField(default=timezone.now)

class Lead(models.Model):
    identity = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    company = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=80, blank=True)
    contact_scope = models.CharField(max_length=16, default="unknown", choices=[("person", "Person"), ("shared", "Shared business"), ("unknown", "Unconfirmed")])
    status = models.CharField(max_length=16, default="new", choices=[("new", "Needs review"), ("reviewed", "Reviewed"), ("rejected", "Rejected"), ("suppressed", "Do not contact")])
    notes = models.TextField(blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    class Meta:
        ordering = ["-last_seen", "name"]

class Observation(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="observations")
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="observations")
    page_url = models.URLField(max_length=1500)
    facts = models.JSONField(default=dict)
    source_category = models.CharField(max_length=40, choices=CATEGORIES)
    person_tags = models.JSONField(default=list)
    company_tags = models.JSONField(default=list)
    evidence = models.TextField()
    method = models.CharField(max_length=20, default="rules")
    content_hash = models.CharField(max_length=64)
    present = models.BooleanField(default=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["lead", "source", "page_url"], name="one_observation_per_lead_page")]
        ordering = ["-last_seen"]

class PageSnapshot(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    url = models.URLField(max_length=1500)
    content_hash = models.CharField(max_length=64)
    extraction_signature = models.CharField(max_length=64)
    last_checked = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "url"], name="one_page_snapshot")]

class SourceCandidate(models.Model):
    url = models.URLField(max_length=1500, unique=True)
    label = models.CharField(max_length=200, blank=True)
    discovered_from = models.ForeignKey(Source, on_delete=models.SET_NULL, null=True)
    evidence_url = models.URLField(max_length=1500)
    status = models.CharField(max_length=16, default="new", choices=[("new", "Needs review"), ("added", "Added"), ("dismissed", "Dismissed")])
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]
