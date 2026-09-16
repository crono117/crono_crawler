"""Authenticated metadata and selector proposals. Never accept remote contact assertions."""
import hashlib
import hmac
import json
from django.core import signing
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from .forms import CandidateForm
from .models import RecipeVersion, SiteAutomationJob
from .policy import authorized
from .services import transition

BUNDLE_SALT = "clearpay.recipe-bundle.v1"


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def sign_bundle(payload, token):
    # Dedicated per-client shared secret; the Django/server secret is never shared.
    return signing.dumps(payload, key=token, salt=BUNDLE_SALT, compress=False)


def verify_bundle(bundle, token, *, source_id, origin, scope_hash):
    payload = signing.loads(bundle, key=token, salt=BUNDLE_SALT, max_age=3600, fallback_keys=[])
    if payload.get("source_id") != source_id or payload.get("origin") != origin or not hmac.compare_digest(payload.get("scope_hash", ""), scope_hash):
        raise ValueError("Recipe bundle does not match the expected source and scope.")
    form = CandidateForm({"recipe": json.dumps(payload["recipe"])})
    if not form.is_valid() or payload.get("status") != "known_good":
        raise ValueError("Invalid recipe bundle.")
    return payload


@transaction.atomic
def propose(job_id, recipe, generation, expected_scope_hash, created_by):
    job = SiteAutomationJob.objects.select_for_update().select_related("source", "campaign").get(pk=job_id)
    if not authorized(job) or job.generation != generation or job.scope_hash != expected_scope_hash:
        raise ValueError("Source scope, generation or policy changed.")
    if job.processing or job.state not in ("paused", "recipe_queued", "probe_complete"):
        raise ValueError("Pause setup before submitting another recipe candidate.")
    pages = job.pages.filter(generation=generation, phase="probe", status="done", expires_at__gt=timezone.now()).exclude(private_key="")
    if not pages.exists():
        raise ValueError("Local probe snapshots are unavailable or expired. Probe again first.")
    form = CandidateForm({"recipe": json.dumps(recipe)})
    if not form.is_valid():
        raise ValueError(form.errors.as_text())
    if job.recipes.filter(generation=generation, status="proposed").count() >= 5:
        raise ValueError("This probe generation already has five external proposals. Probe again to reset the bound.")
    version = (job.recipes.aggregate(last=Max("version"))["last"] or 0) + 1
    candidate = RecipeVersion.objects.create(source=job.source, job=job, version=version, generation=generation,
        recipe=form.cleaned_data["recipe"], scope_hash=job.scope_hash, status="proposed", created_by=created_by[:160])
    transition(job, "recipe_queued", "Candidate received; local evidence validation is required before release.")
    return candidate
