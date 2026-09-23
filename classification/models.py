"""Candidate evidence and optional judgments; never a replacement for accepted leads."""
import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Company(models.Model):
    identity = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.name


class Person(models.Model):
    identity = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    lead = models.ForeignKey('leads.Lead', null=True, blank=True, on_delete=models.SET_NULL)


class EvidenceDocument(models.Model):
    fingerprint = models.CharField(max_length=64, unique=True)
    source = models.ForeignKey('leads.Source', on_delete=models.PROTECT)
    url = models.URLField(max_length=1500)
    retrieved_at = models.DateTimeField(null=True)
    last_checked = models.DateTimeField(default=timezone.now)
    content_hash = models.CharField(max_length=64)
    text_hash = models.CharField(max_length=64)
    scope_hash = models.CharField(max_length=64)
    parser_version = models.CharField(max_length=30)
    extraction_signature = models.CharField(max_length=64, default='')
    text = models.TextField()
    links = models.JSONField(default=list)
    truncated = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True)
    provenance = models.CharField(max_length=30, default='fetched')
    created_at = models.DateTimeField(default=timezone.now)


class EvidenceSpan(models.Model):
    document = models.ForeignKey(EvidenceDocument, on_delete=models.CASCADE, related_name='spans')
    key = models.CharField(max_length=80)
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    text_hash = models.CharField(max_length=64)
    kind = models.CharField(max_length=20)
    locator = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['document', 'key'], name='one_evidence_span_key'),
                       models.CheckConstraint(condition=Q(end__gt=models.F('start')), name='nonempty_evidence_span')]

    @property
    def text(self):
        return self.document.text[self.start:self.end]


class Affiliation(models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='affiliations')
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='affiliations')
    span = models.ForeignKey(EvidenceSpan, on_delete=models.PROTECT)
    title = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, default='candidate')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['person', 'company', 'span'], name='one_affiliation_evidence')]


class CompanyDomain(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='domains')
    url = models.URLField(max_length=1500)
    origin = models.CharField(max_length=500)
    span = models.ForeignKey(EvidenceSpan, on_delete=models.PROTECT)
    state = models.CharField(max_length=20, default='candidate')
    review_note = models.CharField(max_length=1000, blank=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    verified_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'url'], name='one_company_domain_url')]


class ContactCandidate(models.Model):
    span = models.ForeignKey(EvidenceSpan, on_delete=models.PROTECT, related_name='contacts')
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=10)
    value = models.CharField(max_length=254)
    normalized = models.CharField(max_length=254)
    shared_hint = models.BooleanField(default=False)
    association = models.CharField(max_length=20, default='unknown')
    deliverability = models.CharField(max_length=20, default='not_checked')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['span', 'kind', 'value'], name='one_contact_occurrence')]


class Pilot(models.Model):
    name = models.CharField(max_length=160)
    campaign = models.ForeignKey('discovery.Campaign', on_delete=models.PROTECT)
    active = models.BooleanField(default=False)
    max_companies = models.PositiveIntegerField(default=10)
    max_fetches = models.PositiveIntegerField(default=300)
    max_attempts = models.PositiveIntegerField(default=100)
    fetches = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)


OPEN_COMPANY_STATES = ('pending_domain', 'pending_approval', 'queued', 'discovering', 'classifying', 'paused', 'needs_recipe_review')


class CompanyJob(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    pilot = models.ForeignKey(Pilot, on_delete=models.PROTECT, related_name='jobs')
    source = models.ForeignKey('leads.Source', null=True, on_delete=models.PROTECT)
    run = models.ForeignKey('discovery.DiscoveryRun', null=True, on_delete=models.PROTECT)
    key = models.CharField(max_length=64)
    scope_hash = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=24, default='pending_domain')
    reason = models.CharField(max_length=1000, blank=True)
    max_pages = models.PositiveIntegerField(default=10)
    max_fetches = models.PositiveIntegerField(default=30)
    max_attempts = models.PositiveIntegerField(default=10)
    max_evaluations = models.PositiveIntegerField(default=5)
    fetches = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    processing_seconds = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['key'], condition=Q(state__in=OPEN_COMPANY_STATES), name='one_active_company_scope')]


class Evaluation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cache_key = models.CharField(max_length=64, unique=True)
    document = models.ForeignKey(EvidenceDocument, on_delete=models.PROTECT)
    company = models.ForeignKey(Company, null=True, on_delete=models.PROTECT)
    person = models.ForeignKey(Person, null=True, on_delete=models.PROTECT)
    company_job = models.ForeignKey(CompanyJob, null=True, on_delete=models.PROTECT, related_name='evaluations')
    provider = models.CharField(max_length=10)
    catalog_version = models.CharField(max_length=40)
    model = models.CharField(max_length=40)
    actual_model = models.CharField(max_length=40, blank=True)
    request = models.JSONField()
    bindings = models.JSONField(default=dict)
    state = models.CharField(max_length=24, default='queued', db_index=True)
    reason = models.CharField(max_length=1000, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    lease_token = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)


class EvaluationUse(models.Model):
    company_job = models.ForeignKey(CompanyJob, null=True, on_delete=models.PROTECT)
    evaluation = models.ForeignKey(Evaluation, on_delete=models.PROTECT, related_name='uses')
    document = models.ForeignKey(EvidenceDocument, on_delete=models.PROTECT)
    checked_at = models.DateTimeField()
    cache_hit = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)


class Judgment(models.Model):
    evaluation = models.ForeignKey(Evaluation, on_delete=models.PROTECT, related_name='judgments')
    question_id = models.CharField(max_length=100)
    label = models.CharField(max_length=100)
    probabilities = models.JSONField(default=dict)
    confidence = models.FloatField(null=True)
    evidence_ids = models.JSONField(default=list)
    review_state = models.CharField(max_length=20, default='needs_review')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['evaluation', 'question_id'], name='one_judgment_question')]


class JevControl(models.Model):
    key = models.CharField(max_length=20, primary_key=True, default='jev')
    paused = models.BooleanField(default=False)
    reason = models.CharField(max_length=1000, blank=True)
    allowance_nusd = models.PositiveBigIntegerField(default=1_000_000_000)
    cumulative_attempt_limit = models.PositiveIntegerField(null=True, blank=True)
    spent_nusd = models.PositiveBigIntegerField(default=0)
    reserved_nusd = models.PositiveBigIntegerField(default=0)
    next_allowed_at = models.DateTimeField(default=timezone.now)
    active_attempt = models.UUIDField(null=True)

    @property
    def remaining_nusd(self):
        return self.allowance_nusd - self.spent_nusd - self.reserved_nusd


class DailyUsage(models.Model):
    day = models.DateField(unique=True)
    attempts = models.PositiveIntegerField(default=0)
    succeeded = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)


class Attempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation = models.ForeignKey(Evaluation, on_delete=models.PROTECT, related_name='attempt_history')
    ordinal = models.PositiveIntegerField()
    day = models.DateField()
    state = models.CharField(max_length=20, default='reserved')
    lease_token = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    reserved_nusd = models.PositiveBigIntegerField()
    cost_nusd = models.PositiveBigIntegerField(null=True)
    unit_price_nusd = models.PositiveIntegerField(default=42)
    input_tokens = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    estimated_tokens = models.PositiveIntegerField()
    counter = models.CharField(max_length=200)
    http_status = models.PositiveIntegerField(null=True)
    request_id = models.CharField(max_length=200, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    latency_ms = models.PositiveIntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['evaluation', 'ordinal'], name='one_evaluation_attempt')]


class ControlEvent(models.Model):
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=40)
    reason = models.CharField(max_length=1000)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class Lineage(models.Model):
    company_job = models.ForeignKey(CompanyJob, null=True, on_delete=models.PROTECT, related_name='lineage')
    evaluation = models.ForeignKey(Evaluation, null=True, on_delete=models.PROTECT)
    document = models.ForeignKey(EvidenceDocument, on_delete=models.PROTECT)
    url = models.CharField(max_length=1500, blank=True)
    relation = models.CharField(max_length=30)
    created_at = models.DateTimeField(default=timezone.now)
