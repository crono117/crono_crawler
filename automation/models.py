from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


DEFAULT_PATHS = "/team\n/our-team\n/staff\n/people\n/sales\n/agents\n/representatives\n/reps\n/partners\n/contact\n/contact-us\n/about\n/about-us\n/executive-team\n/leadership\n/independent-sales"
DEFAULT_DENY = "facebook.com\nlinkedin.com\nx.com\ntwitter.com\ninstagram.com\ntiktok.com\nyoutube.com\nreddit.com"


def bounded(default, low, high):
    return models.PositiveIntegerField(default=default, validators=[MinValueValidator(low), MaxValueValidator(high)])


class SitePolicy(models.Model):
    campaign = models.OneToOneField("discovery.Campaign", on_delete=models.CASCADE, related_name="site_policy")
    enabled = models.BooleanField(default=False)
    min_url_score = bounded(70, 35, 100)
    min_recipe_score = bounded(85, 85, 90)
    allowed_paths = models.TextField(default=DEFAULT_PATHS, max_length=3000)
    allow_homepage = models.BooleanField(default=True)
    denied_domains = models.TextField(default=DEFAULT_DENY, max_length=3000)
    max_sites_per_day = bounded(3, 1, 25)
    probe_pages = bounded(3, 1, 5)
    canary_pages = bounded(5, 1, 10)
    delay_seconds = bounded(5, 2, 3600)
    recheck_days = bounded(7, 1, 90)
    notes = models.TextField(blank=True, max_length=3000)
    pending_scan_cursor = models.PositiveBigIntegerField(default=0, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class SiteAutomationJob(models.Model):
    STATES = [(s, s.replace("_", " ").title()) for s in (
        "probe_queued", "probing", "probe_complete", "recipe_queued", "recipe_testing",
        "recipe_ready", "recipe_released", "canary", "active", "paused", "failed")]
    source = models.OneToOneField("leads.Source", on_delete=models.CASCADE, related_name="automation_job")
    campaign = models.ForeignKey("discovery.Campaign", on_delete=models.PROTECT, related_name="automation_jobs")
    state = models.CharField(max_length=24, choices=STATES, default="probe_queued")
    policy_snapshot = models.JSONField(default=dict)
    scope_hash = models.CharField(max_length=64)
    generation = models.PositiveIntegerField(default=1)
    recon = models.JSONField(default=dict)
    current_recipe = models.ForeignKey("RecipeVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="current_jobs")
    last_good_recipe = models.ForeignKey("RecipeVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="healthy_jobs")
    processing = models.BooleanField(default=False)
    lease_token = models.CharField(max_length=64, blank=True)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    next_probe_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class ProbePage(models.Model):
    job = models.ForeignKey(SiteAutomationJob, on_delete=models.CASCADE, related_name="pages")
    generation = models.PositiveIntegerField(default=1)
    phase = models.CharField(max_length=10, default="probe")
    url = models.URLField(max_length=1500)
    depth = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default="queued")
    available_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict)
    private_key = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    message = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["job", "generation", "phase", "url"], name="one_probe_page_per_generation")]


class RecipeVersion(models.Model):
    source = models.ForeignKey("leads.Source", on_delete=models.PROTECT, related_name="recipe_versions")
    job = models.ForeignKey(SiteAutomationJob, on_delete=models.PROTECT, related_name="recipes")
    version = models.PositiveIntegerField()
    generation = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default="candidate")
    recipe = models.JSONField(default=dict)
    validation = models.JSONField(default=dict)
    score = models.PositiveIntegerField(default=0)
    scope_hash = models.CharField(max_length=64)
    probe_version = models.CharField(max_length=20, default="1.0")
    created_by = models.CharField(max_length=160, default="deterministic worker")
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["source", "version"], name="one_recipe_version_per_source")]


class AutomationEvent(models.Model):
    job = models.ForeignKey(SiteAutomationJob, on_delete=models.CASCADE, related_name="events")
    state = models.CharField(max_length=24)
    message = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class CoordinatorClient(models.Model):
    name = models.CharField(max_length=120)
    campaign = models.ForeignKey("discovery.Campaign", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
