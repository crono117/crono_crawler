import json
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST
from discovery.models import Campaign
from leads.views import staff_required
from .forms import CandidateForm, PolicyForm
from .models import SiteAutomationJob, SitePolicy
from .services import pause_job, start_setup

BRIDGE_STATE_LABELS = {
    "probe_queued": "Awaiting launch",
    "probing": "Crawling",
    "probe_complete": "Probe complete",
    "recipe_queued": "Building recipe",
    "recipe_testing": "Testing recipe",
    "recipe_ready": "Recipe ready",
    "recipe_released": "Recipe released",
    "canary": "Canary run",
    "paused": "Needs review",
    "failed": "Failed",
}


@staff_required
def home(request):
    from collections import Counter
    from .models import SiteAssessment
    jobs = SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe")
    assessments = list(SiteAssessment.objects.all()[:500])
    reasons, options = Counter(), Counter()
    for item in assessments:
        reasons.update(item.reasons)
        options.update(item.future_options)
    return render(request, "automation/home.html", {"jobs": jobs[:100], "campaigns": Campaign.objects.all(),
        "active_count": jobs.filter(state="active").count(), "review_count": jobs.filter(state__in=("paused", "failed")).count(),
        "assessment_outcomes": Counter(item.outcome for item in assessments).most_common(),
        "assessment_reasons": reasons.most_common(10), "assessment_options": options.most_common(),
        "recent_assessments": assessments[:25]})


@staff_required
def policy(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id)
    instance = SitePolicy.objects.filter(campaign=campaign).first() or SitePolicy(campaign=campaign)
    form = PolicyForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            for job in campaign.automation_jobs.select_related("source"):
                pause_job(job, "Campaign automation policy changed; setup requires revalidation.", rollback=False)
        messages.success(request, "Automation policy saved. New qualifying sites use it when the campaign runs. Existing setup jobs are paused for policy review.")
        return redirect("automation:home")
    return render(request, "automation/policy.html", {"campaign": campaign, "form": form})


@staff_required
def job_detail(request, pk):
    job = get_object_or_404(SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe"), pk=pk)
    return render(request, "automation/job.html", {"job": job, "versions": job.recipes.all()[:20],
        "events": job.events.all()[:30], "recon_json": json.dumps(job.recon, indent=2),
        "pages": job.pages.filter(generation=job.generation),
        "form": CandidateForm(initial={"recipe": job.current_recipe.recipe if job.current_recipe else {}})})


@require_POST
@staff_required
def job_action(request, pk):
    job = get_object_or_404(SiteAutomationJob.objects.select_related("source", "campaign"), pk=pk)
    try:
        if request.POST.get("action") == "pause":
            pause_job(job, "Paused by operator.", rollback=False)
        elif request.POST.get("action") == "rollback":
            pause_job(job, "Operator rollback; source remains paused until revalidated.")
        elif request.POST.get("action") == "probe":
            start_setup(job.source, job.campaign)
        elif request.POST.get("action") == "candidate":
            from .coordination import propose
            form = CandidateForm(request.POST)
            if not form.is_valid():
                raise ValueError(form.errors.as_text())
            propose(job.pk, form.cleaned_data["recipe"], job.generation, job.scope_hash, request.user.username)
        messages.success(request, "Setup action recorded.")
    except (ValueError, SitePolicy.DoesNotExist) as exc:
        messages.error(request, str(exc) or "Configure an enabled automation policy first.")
    return redirect("automation:job", pk=pk)


@staff_required
def bridge(request):
    return render(request, "automation/bridge.html", {})


@require_GET
@staff_required
def bridge_status(request):
    from datetime import timedelta
    from django.urls import reverse
    from django.utils import timezone
    from leads.models import Run, WorkerLease

    lease = WorkerLease.objects.filter(key="collector").first()
    worker_online = bool(lease and lease.token and lease.heartbeat_at > timezone.now() - timedelta(minutes=10))

    stations = []
    setups = (SiteAutomationJob.objects.exclude(state="active")
              .select_related("source").order_by("-id")[:10])
    for job in setups:
        room = "review" if job.state in ("paused", "failed") else "automation"
        stations.append({"key": f"job-{job.pk}", "site": job.source.name or job.source.url, "room": room,
            "state": job.state, "label": BRIDGE_STATE_LABELS.get(job.state, job.get_state_display()),
            "message": job.message[:140], "href": reverse("automation:job", args=[job.pk])})

    for run in Run.objects.filter(status="running").select_related("source").order_by("-id")[:10]:
        stations.append({"key": f"run-{run.pk}", "site": run.source.name or run.source.url, "room": "collection",
            "state": "collecting", "label": "Crawling",
            "message": f"{run.pages_done} page(s), {run.contacts_seen} contact(s) so far",
            "href": reverse("run_detail", args=[run.pk])})

    from discovery.models import DiscoveredURL
    from leads.models import SourceCandidate
    cargo_count = (SourceCandidate.objects.filter(status="new").count()
                   + DiscoveredURL.objects.filter(decision="pending").count())

    return JsonResponse({"worker_online": worker_online, "stations": stations,
        "cargo_count": cargo_count, "cargo_href": reverse("discovery:candidates")})


@require_POST
@staff_required
def source_setup(request, campaign_id, source_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id)
    source = get_object_or_404(campaign.sources, pk=source_id)
    try:
        job = start_setup(source, campaign)
        return redirect("automation:job", pk=job.pk)
    except (ValueError, SitePolicy.DoesNotExist) as exc:
        messages.error(request, str(exc) or "Configure an enabled automation policy first.")
        return redirect("automation:policy", campaign_id=campaign.pk)
