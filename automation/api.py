import json
from functools import wraps
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from leads.services.network import origin
from .coordination import propose, sign_bundle, token_hash
from .models import CoordinatorClient, SiteAutomationJob
from .policy import collection_allowed


def client_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        # No header-trusting HTTPS bypass. Loopback is for the local pilot only.
        if not request.is_secure() and request.META.get("REMOTE_ADDR") not in ("127.0.0.1", "::1"):
            return JsonResponse({"error": "HTTPS is required."}, status=403)
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        client = CoordinatorClient.objects.filter(token_hash=token_hash(token), active=True).first() if len(token) >= 32 else None
        if client is None:
            return JsonResponse({"error": "Authentication required."}, status=401)
        request.coordinator, request.coordinator_token = client, token
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response
    return wrapped


def client_job(request, pk):
    return SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe").filter(
        pk=pk, campaign_id=request.coordinator.campaign_id).first()


@require_GET
@client_required
def jobs(request):
    items = SiteAutomationJob.objects.filter(campaign_id=request.coordinator.campaign_id).order_by("id")[:100]
    return JsonResponse({"jobs": [{"id": j.pk, "source_id": j.source_id, "state": j.state,
        "generation": j.generation, "scope_hash": j.scope_hash} for j in items]})


@require_GET
@client_required
def recon(request, pk):
    job = client_job(request, pk)
    if not job:
        return JsonResponse({"error": "Job not found."}, status=404)
    return JsonResponse(job.recon)


@require_GET
@client_required
def bundle(request, pk):
    job = client_job(request, pk)
    if not job:
        return JsonResponse({"error": "Job not found."}, status=404)
    if not collection_allowed(job.source):
        return JsonResponse({"error": "No active known-good recipe."}, status=409)
    version = job.current_recipe
    payload = {"schema_version": 1, "source_id": job.source_id, "origin": origin(job.source.url),
        "scope": job.source.allowed_paths.splitlines(), "allow_homepage": job.source.allow_homepage,
        "scope_hash": job.scope_hash, "version": version.version, "status": version.status,
        "recipe": version.recipe, "validation_score": version.score,
        "probe_version": version.probe_version, "content_hashes": version.validation.get("content_hashes", {})}
    return JsonResponse({"bundle": sign_bundle(payload, request.coordinator_token), "expires_in_seconds": 3600})


@csrf_exempt
@require_POST
@client_required
def candidate(request, pk):
    job = client_job(request, pk)
    if not job:
        return JsonResponse({"error": "Job not found."}, status=404)
    if len(request.body) > 16000:
        return JsonResponse({"error": "Request too large."}, status=413)
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict) or set(data) != {"recipe", "generation", "scope_hash"}:
            raise ValueError("Send only recipe, generation and scope_hash. Raw HTML and contacts are not accepted.")
        result = propose(job.pk, data["recipe"], data["generation"], data["scope_hash"], request.coordinator.name)
    except (ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"status": "candidate", "version": result.version}, status=202)
