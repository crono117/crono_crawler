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
    require_page_intent = models.BooleanField(
        default=True,
        help_text="Require the candidate URL/label to already look like a team, staff or sales-role page "
                  "before automatic setup. Turning this off lets bare company homepages qualify too, relying "
                  "on the unchanged probe/recipe/canary gates as the safety check instead.",
    )
    denied_domains = models.TextField(default=DEFAULT_DENY, max_length=3000)
    max_sites_per_day = bounded(3, 1, 25)
    # Admission-only (see policy.ADMISSION_FIELDS). Percent of max_sites_per_day that candidates WITHOUT
    # direct team/person-page intent (about/contact pages and bare homepages) may take. 100 keeps the
    # historical first-eligible behavior; lower values reserve the rest for direct-intent pages and
    # hand the exploratory slots to the best-ranked sites instead of whichever arrived first.
    explore_share = models.PositiveIntegerField(
        default=100, validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percent of the daily automatic-setup allowance open to candidates without direct team/person-page "
                  "intent. 100 = no reservation (first eligible wins).")
    probe_pages = bounded(3, 1, 5)
    canary_pages = bounded(5, 1, 10)
    delay_seconds = bounded(5, 2, 3600)
    recheck_days = bounded(7, 1, 90)
    notes = models.TextField(blank=True, max_length=3000)
    pending_scan_cursor = models.PositiveBigIntegerField(default=0, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class SiteAutomationJob(models.Model):
    company_job = models.ForeignKey("classification.CompanyJob", null=True, blank=True, on_delete=models.PROTECT)
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
    # Canonical onboarding: the URL the policy originally authorized, and how many times setup
    # rebound the source to a same-site canonical form (http->https, www<->bare). Bounded.
    original_url = models.URLField(max_length=1500, blank=True)
    canonical_rebinds = models.PositiveSmallIntegerField(default=0)
    # Why this site took a daily setup slot: intake tier (direct/company/generic, blank for jobs admitted
    # before ranking existed), how it was found, and the explainable rank. Durable outcomes are joined back
    # to these so query and tier yield can be learned from (see automation.learning).
    admission_tier = models.CharField(max_length=10, blank=True)
    admission_method = models.CharField(max_length=20, blank=True)
    admission_query = models.CharField(max_length=600, blank=True)
    admission_rank = models.IntegerField(null=True, blank=True)
    admission_reasons = models.JSONField(default=list, blank=True)
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


class SiteAssessment(models.Model):
    """Durable record of why a probed site did or didn't yield contacts, kept for later analysis.

    One row per job generation, so re-probes add history instead of overwriting it. Holds
    aggregate counts and reason codes only: never names, emails or phone numbers.
    """
    job = models.ForeignKey(SiteAutomationJob, null=True, blank=True, on_delete=models.SET_NULL, related_name="assessments")
    source = models.ForeignKey("leads.Source", null=True, blank=True, on_delete=models.SET_NULL, related_name="assessments")
    origin = models.CharField(max_length=300)
    generation = models.PositiveIntegerField(default=1)
    outcome = models.CharField(max_length=40)
    reasons = models.JSONField(default=list)
    future_options = models.JSONField(default=list)
    facts = models.JSONField(default=dict)
    summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["job", "generation"], name="one_assessment_per_job_generation")]


class CoordinatorClient(models.Model):
    name = models.CharField(max_length=120)
    campaign = models.ForeignKey("discovery.Campaign", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
