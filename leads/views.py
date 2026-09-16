import csv
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .forms import LeadReviewForm, SourceForm
from .models import CATEGORIES, Lead, Observation, PageJob, Run, Source, SourceCandidate, WorkerLease
from .services.worker import enqueue, pause_source
from .services.health import recipe_review_runs

staff_required = user_passes_test(lambda u: u.is_authenticated and u.is_active and u.is_staff)

def console_context(request):
    if not request.user.is_authenticated:
        return {}
    lease = WorkerLease.objects.filter(key="collector").first()
    return {"worker_online": bool(lease and lease.token and lease.heartbeat_at > timezone.now() - timedelta(minutes=10)),
            "worker_heartbeat": lease.heartbeat_at if lease else None, "categories": CATEGORIES,
            "ollama_enabled": bool(settings.OLLAMA_MODEL)}

@staff_required
def dashboard(request):
    from discovery.models import DiscoveredURL, DiscoveryRun
    return render(request, "leads/dashboard.html", {
        "lead_count": Lead.objects.exclude(status__in=("rejected", "suppressed")).count(),
        "review_count": Lead.objects.filter(status="new").count(),
        "active_sources": Source.objects.filter(active=True, approved=True).count(),
        "candidate_count": SourceCandidate.objects.filter(status="new").count() + DiscoveredURL.objects.filter(decision="pending").count(),
        "recent_leads": Lead.objects.prefetch_related("observations__source")[:5],
        "recent_runs": Run.objects.select_related("source")[:6],
        "source_count": Source.objects.count(),
        "collection_alerts": recipe_review_runs(Run, "source")[:5],
        "discovery_alerts": recipe_review_runs(DiscoveryRun, "campaign")[:5],
    })

@staff_required
def sources(request):
    return render(request, "leads/sources.html", {"sources": Source.objects.all()})

@staff_required
def source_form(request, pk=None):
    from discovery.models import DiscoveredURL
    from discovery.services import approve, pause_for_source
    source = get_object_or_404(Source, pk=pk) if pk else None
    if source and source.collector == "demo":
        messages.info(request, "The offline fixture has a fixed configuration. Add a new source to collect a real website.")
        return redirect("source_detail", pk=source.pk)
    candidate = SourceCandidate.objects.filter(pk=request.GET.get("candidate")).first() if request.GET.get("candidate", "").isdigit() else None
    initial = {"url": candidate.url, "name": candidate.label or candidate.url} if candidate else {}
    discovered = get_object_or_404(DiscoveredURL, pk=request.GET["discovery_url"]) if request.GET.get("discovery_url", "").isdigit() else None
    if discovered and not source:
        initial = {"url": discovered.url, "name": (discovered.label or discovered.origin)[:160],
                   "category": discovered.campaign.category, "allowed_paths": "/"}
    form = SourceForm(request.POST or None, instance=source, initial=initial)
    if request.method == "POST" and discovered and form.is_valid():
        from leads.services.network import in_scope
        if not in_scope(form.instance, discovered.url):
            form.add_error("allowed_paths", "The source URL and scope must cover the discovered URL you are reviewing.")
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            source = form.save(commit=False)
            # Editing configuration stops the old crawl so new limits apply consistently.
            if source.pk:
                from automation.models import SiteAutomationJob
                from automation.services import pause_job
                setup_job = SiteAutomationJob.objects.filter(source=source).first()
                if setup_job:
                    pause_job(setup_job, "Source settings changed; review and re-probe before release.", rollback=False)
                pause_for_source(source)
                Run.objects.filter(source=source, status__in=("queued", "running", "paused")).update(
                    status="cancelled", finished_at=timezone.now(), message="Source configuration changed.")
            source.active = False
            source.approval_kind = "operator"
            source.save()
            if candidate:
                candidate.status = "added"
                candidate.save()
            if discovered and source.approved:
                approve(discovered, source)
        messages.success(request, "Source saved. Use Run now to begin collection.")
        return redirect("source_detail", pk=source.pk)
    return render(request, "leads/source_form.html", {"form": form, "source": source})

@staff_required
def source_detail(request, pk):
    source = get_object_or_404(Source, pk=pk)
    return render(request, "leads/source_detail.html", {"source": source, "runs": source.runs.all()[:15],
        "collection_alerts": recipe_review_runs(Run, "source").filter(source=source),
        "observations": source.observations.select_related("lead")[:20]})

@require_POST
@staff_required
def source_action(request, pk):
    source = get_object_or_404(Source, pk=pk)
    action = request.POST.get("action")
    if action == "pause":
        pause_source(source)
        messages.success(request, "Paused. An in-flight page may finish saving before the worker stops this source.")
    elif action == "run":
        try:
            run = enqueue(source)
            messages.success(request, f"Run #{run.pk} queued. Recurring collection is enabled.")
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect("source_detail", pk=source.pk)

def filtered_leads(request):
    qs = Lead.objects.prefetch_related("observations__source")
    query = request.GET.get("q", "").strip()
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(company__icontains=query) | Q(title__icontains=query) | Q(email__icontains=query))
    status = request.GET.get("status", "")
    if status in dict(Lead._meta.get_field("status").choices):
        qs = qs.filter(status=status)
    category = request.GET.get("category", "")
    if category in dict(CATEGORIES):
        qs = qs.filter(observations__source_category=category)
    service = request.GET.get("service", "")
    if service in dict(CATEGORIES):
        qs = qs.filter(observations__person_tags__icontains=service)
    return qs.distinct()

@staff_required
def lead_list(request):
    page = Paginator(filtered_leads(request), 30).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "leads/leads.html", {"page": page, "querystring": params.urlencode(),
        "statuses": Lead._meta.get_field("status").choices})

@staff_required
def lead_detail(request, pk):
    lead = get_object_or_404(Lead.objects.prefetch_related("observations__source"), pk=pk)
    form = LeadReviewForm(request.POST or None, instance=lead)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Review saved.")
        return redirect("lead_detail", pk=pk)
    return render(request, "leads/lead_detail.html", {"lead": lead, "form": form})

def safe_cell(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value

@staff_required
def export_leads(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="clearpay-leads.csv"'
    writer = csv.writer(response)
    writer.writerow(["name", "company", "title", "email", "phone", "contact_scope", "review_status",
                     "source_business_types", "person_services", "company_services", "source_urls", "last_seen", "notes"])
    for lead in filtered_leads(request).exclude(status__in=("suppressed", "rejected")).iterator(chunk_size=200):
        observations = list(lead.observations.all())
        row = [lead.name, lead.company, lead.title, lead.email, lead.phone, lead.contact_scope, lead.status,
               "; ".join(sorted({o.get_source_category_display() for o in observations})),
               "; ".join(sorted({tag for o in observations for tag in o.person_tags})),
               "; ".join(sorted({tag for o in observations for tag in o.company_tags})),
               "; ".join(sorted({o.page_url for o in observations})), lead.last_seen.isoformat(), lead.notes]
        writer.writerow([safe_cell(v) for v in row])
    return response

@staff_required
def runs(request):
    return render(request, "leads/runs.html", {"page": Paginator(Run.objects.select_related("source"), 30).get_page(request.GET.get("page"))})

@staff_required
def run_detail(request, pk):
    run = get_object_or_404(Run.objects.select_related("source"), pk=pk)
    return render(request, "leads/run_detail.html", {"run": run, "jobs": run.jobs.all()})

@staff_required
def candidates(request):
    return render(request, "leads/candidates.html", {"page": Paginator(SourceCandidate.objects.filter(status="new").select_related("discovered_from"), 30).get_page(request.GET.get("page"))})

@require_POST
@staff_required
def candidate_dismiss(request, pk):
    candidate = get_object_or_404(SourceCandidate, pk=pk)
    candidate.status = "dismissed"
    candidate.save()
    return redirect("candidates")
