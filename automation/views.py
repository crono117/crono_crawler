import json
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from discovery.models import Campaign
from leads.views import staff_required
from .forms import CandidateForm, PolicyForm
from .models import SiteAutomationJob, SitePolicy
from .services import pause_job, start_setup


@staff_required
def home(request):
    jobs = SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe")
    return render(request, "automation/home.html", {"jobs": jobs[:100], "campaigns": Campaign.objects.all(),
        "active_count": jobs.filter(state="active").count(), "review_count": jobs.filter(state__in=("paused", "failed")).count()})


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
