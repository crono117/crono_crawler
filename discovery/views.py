from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from leads.views import staff_required
from leads.services.health import recipe_review_runs
from .forms import ApproveURLForm, CampaignForm
from .models import Campaign, DailyUsage, DiscoveredURL, DiscoveryRun
from .providers import search_ready
from .services import OPEN, approve, pause, refresh_priorities, start


@staff_required
def home(request):
    return render(request, "discovery/home.html", {
        "campaigns": Campaign.objects.prefetch_related("sources"),
        "pending_count": DiscoveredURL.objects.filter(decision="pending").count(),
        "url_count": DiscoveredURL.objects.count(),
        "active_count": Campaign.objects.filter(active=True).count(),
        "today": DailyUsage.objects.filter(day=timezone.now().date()).aggregate(requests=Sum("requests"), searches=Sum("searches")),
        "search_ready": search_ready(), "recent_runs": DiscoveryRun.objects.select_related("campaign")[:10],
        "discovery_alerts": recipe_review_runs(DiscoveryRun, "campaign")[:5],
    })


@staff_required
def campaign_form(request, pk=None):
    campaign = get_object_or_404(Campaign, pk=pk) if pk else None
    form = CampaignForm(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            campaign = form.save(commit=False)
            if campaign.pk:
                campaign.runs.filter(status__in=OPEN).update(status="cancelled", finished_at=timezone.now(), message="Campaign settings changed.")
            campaign.active = False
            campaign.save()
            form.save_m2m()
            refresh_priorities(campaign)
        messages.success(request, "Campaign saved and paused. Start it when ready.")
        return redirect("discovery:campaign", pk=campaign.pk)
    return render(request, "discovery/form.html", {"form": form, "campaign": campaign})


@staff_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign.objects.prefetch_related("sources"), pk=pk)
    return render(request, "discovery/campaign.html", {"campaign": campaign, "runs": campaign.runs.all()[:20],
        "discovery_alerts": recipe_review_runs(DiscoveryRun, "campaign").filter(campaign=campaign),
        "url_count": campaign.urls.count(), "pending_count": campaign.urls.filter(decision="pending").count(),
        "today": campaign.usage.filter(day=timezone.now().date()).first()})


@require_POST
@staff_required
def campaign_action(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    action = request.POST.get("action")
    if action == "pause":
        pause(campaign)
        messages.success(request, "Campaign paused. An in-flight page may finish saving.")
    elif action == "start":
        try:
            run = start(campaign)
            messages.success(request, f"Discovery run #{run.pk} queued; recurring discovery is enabled.")
        except ValueError as exc:
            messages.error(request, str(exc))
    elif action == "cancel":
        with transaction.atomic():
            pause(campaign)
            campaign.runs.filter(status__in=OPEN).update(status="cancelled", finished_at=timezone.now(), message="Cancelled by operator.")
        messages.success(request, "Open run cancelled; saved URLs and contacts are retained.")
    return redirect("discovery:campaign", pk=pk)


@staff_required
def candidates(request):
    urls = DiscoveredURL.objects.select_related("campaign", "source")
    decision = request.GET.get("decision", "pending")
    if decision in ("pending", "approved", "dismissed"):
        urls = urls.filter(decision=decision)
    if request.GET.get("campaign", "").isdigit():
        urls = urls.filter(campaign_id=request.GET["campaign"])
    query = request.GET.get("q", "").strip()
    if query:
        urls = urls.filter(Q(url__icontains=query) | Q(label__icontains=query))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "discovery/candidates.html", {"page": Paginator(urls, 30).get_page(request.GET.get("page")),
        "campaigns": Campaign.objects.all(), "decision": decision, "querystring": params.urlencode()})


@staff_required
def candidate_detail(request, pk):
    candidate = get_object_or_404(DiscoveredURL.objects.select_related("campaign", "source"), pk=pk)
    form = ApproveURLForm(candidate, request.POST or None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("dismiss", "dismiss_origin"):
            targets = DiscoveredURL.objects.filter(pk=pk) if action == "dismiss" else candidate.campaign.urls.filter(origin=candidate.origin)
            targets.update(decision="dismissed")
            messages.success(request, "Dismissed. Queued requests for these URLs will be skipped.")
            return redirect("discovery:candidates")
        if action == "approve" and form.is_valid():
            approve(candidate, form.cleaned_data["source"])
            messages.success(request, "Approved scope attached to this campaign. Eligible URLs enter the current run if capacity remains, or the next run.")
            return redirect("discovery:campaign", pk=candidate.campaign_id)
    return render(request, "discovery/candidate.html", {"candidate": candidate, "form": form, "jobs": candidate.jobs.select_related("run")[:15]})


@staff_required
def run_detail(request, pk):
    run = get_object_or_404(DiscoveryRun.objects.select_related("campaign"), pk=pk)
    return render(request, "discovery/run.html", {"run": run,
        "page": Paginator(run.jobs.select_related("candidate", "source"), 50).get_page(request.GET.get("page"))})
