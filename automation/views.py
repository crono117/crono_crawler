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


LAB_TILESET_DIR = "local_assets/neo-zero"
LAB_ROBOTS_DIR = "local_assets/robots"


def _local_sheets(relative):
    from pathlib import Path
    from django.conf import settings
    from django.templatetags.static import static
    folder = Path(settings.BASE_DIR) / "static" / relative
    return {path.stem: static(f"{relative}/{path.relative_to(folder).as_posix()}")
            for path in sorted(folder.rglob("*.png"))} if folder.is_dir() else {}


@require_GET
@staff_required
def lab(request):
    from django.conf import settings
    # Licensed art (Neo Zero tiles, sci-fi robot sprites) can't be redistributed, so it is never
    # committed: an operator drops their own download under static/local_assets/ and the Lab uses it.
    from pathlib import Path
    # Cache-bust the lab scripts: a browser holding an older lab.js would misread a newer feed.
    scripts = Path(settings.BASE_DIR) / "static" / "lab"
    version = int(max(path.stat().st_mtime for path in scripts.glob("*.js")))
    return render(request, "automation/lab.html", {"tileset_sheets": _local_sheets(LAB_TILESET_DIR),
                                                   "lab_version": version,
                                                   "robot_sheets": _local_sheets(LAB_ROBOTS_DIR),
                                                   "activity_enabled": bool(settings.ACTIVITY_LOG)})


@require_GET
@staff_required
def lab_feed(request):
    from datetime import timedelta
    from django.utils import timezone
    from classification.models import Evaluation
    from discovery.models import DiscoveredURL, DiscoveryJob
    from leads.models import PageJob, SourceCandidate, WorkerLease
    from leads.services import activity
    from .services import RUNNABLE

    try:
        after = float(request.GET.get("after", 0))
    except ValueError:
        after = 0.0
    entries = activity.read(after=after, limit=300 if after else 120)
    lease = WorkerLease.objects.filter(key="collector").first()
    now = timezone.now()
    from django.conf import settings
    # Same filters the worker's tick() uses, so "ready" means the worker could pick it up now.
    pending = {
        "collector": PageJob.objects.filter(status="queued", run__status__in=("queued", "running"),
                                            run__source__active=True, run__source__approved=True),
        "discovery": DiscoveryJob.objects.filter(status="queued", run__status__in=("queued", "running"),
                                                 run__campaign__active=True),
        "automation": SiteAutomationJob.objects.filter(state__in=RUNNABLE, processing=False, campaign__active=True,
                                                       campaign__site_policy__enabled=True),
        "jev": Evaluation.objects.filter(provider=settings.JEV_MODE, state__in=("queued", "retry_wait", "waiting")),
    }
    queues = {}
    for lane, items in pending.items():
        next_at = items.filter(available_at__gt=now).order_by("available_at").values_list("available_at", flat=True).first()
        queues[lane] = {"queued": items.count(), "ready": items.filter(available_at__lte=now).count(),
                        "next_at": next_at.timestamp() if next_at else None}
    if settings.JEV_MODE == "off":
        queues["jev"]["note"] = "Jev is off (JEV_MODE=off)."
    return JsonResponse({
        "entries": entries,
        "worker_online": bool(lease and lease.token and lease.heartbeat_at > now - timedelta(minutes=10)),
        "queues": queues,
        "approvals": (SourceCandidate.objects.filter(status="new").count()
                      + DiscoveredURL.objects.filter(decision="pending").count()),
        "server_time": now.timestamp(),
    })


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
