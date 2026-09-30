"""Job-by-job audit of automatic source setup. Read-only; no network requests.

Groups every SiteAutomationJob of a campaign into the funnel stage where it stopped, classifies
redirect failures with automation.canonical.classify, and replays two policy questions against
stored data only: would require_page_intent have admitted the job, and was an accepted recipe
below the 85 readiness floor the policy model now enforces. Output holds IDs, origins, paths,
codes and counts only: never names, emails, phone numbers or page HTML.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Max
from django.utils import timezone

from automation.canonical import bare, classify, redirect_target
from automation.models import ProbePage, SiteAutomationJob, SitePolicy
from automation.policy import policy_scope
from discovery.models import Campaign
from discovery.ranking import direct_contact_page_intent, rank
from leads.models import Observation
from leads.services.network import in_scope, origin

MODEL_RECIPE_FLOOR = 85
CARDS_WITHOUT_SALES = {"people_without_personal_contacts", "contacts_rejected", "non_sales_titles_only",
                       "contact_details_without_people", "only_shared_company_contact",
                       "no_contact_details_published", "structured_people_without_employer"}


def stage(job, assessment, page_messages):
    """(category, detail) for where setup stopped, most specific evidence first."""
    outcome = assessment.outcome if assessment else ""
    reasons = set(assessment.reasons if assessment else [])
    text = " ".join([job.message] + page_messages).lower()
    if job.state == "active":
        return "active", "canary_passed"
    if job.state not in ("paused", "failed"):
        return "in_progress", job.state
    if outcome == "canonical_conflict" or "canonical origin conflict" in text:
        return "redirect_scope", "canonical_conflict"
    if outcome == "redirect_out_of_scope" or "outside the approved scope" in text:
        return "redirect_scope", "redirect"
    if "robots.txt returned html" in text:
        return "access", "robots_returned_html"
    if outcome == "robots_disallowed" or "robots" in text:
        return "access", "robots_disallowed"
    if "access challenge" in text:
        return "access", "access_challenge"
    if outcome == "blocked" or "http 401" in text or "http 403" in text:
        return "access", "http_401_403"
    if "size limit" in text:
        return "access", "oversized"
    if outcome in ("unavailable", "collection_prohibited") or "page unavailable" in text or "dns lookup" in text:
        return "access", outcome or "unavailable"
    if "no_team_about_or_contact_page_linked" in reasons or outcome in ("no_people_listed", "no_pages"):
        return "no_people_page", outcome or "no_people_listed"
    if outcome in CARDS_WITHOUT_SALES or reasons & CARDS_WITHOUT_SALES:
        return "cards_without_sales_records", outcome
    if "no_matching_cards" in text or "ambiguous_card_layout" in reasons:
        return "recipe_segmentation", "no_matching_cards" if "no_matching_cards" in text else "ambiguous_card_layout"
    if "recipe review needed" in text or "release threshold" in text or "canary" in text:
        return "recipe_segmentation", "recipe_or_canary_gate"
    return "other", outcome or "unclassified"


class Command(BaseCommand):
    help = "Audit automatic source setup jobs for one campaign (read-only JSON)."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--since", help="Only jobs created on or after this date or ISO timestamp (UTC).")
        parser.add_argument("--no-jobs", action="store_true", help="Print the summary only.")

    def handle(self, *args, **options):
        try:
            campaign = Campaign.objects.get(pk=options["campaign"])
        except Campaign.DoesNotExist as exc:
            raise CommandError("Campaign does not exist.") from exc
        policy = SitePolicy.objects.filter(campaign=campaign).first()
        jobs = SiteAutomationJob.objects.filter(campaign=campaign).select_related("source", "current_recipe",
                                                                                 "last_good_recipe")
        if options["since"]:
            try:
                since = datetime.fromisoformat(options["since"])
            except ValueError as exc:
                raise CommandError("--since must be an ISO date or timestamp.") from exc
            if since.tzinfo is None:
                since = since.replace(tzinfo=dt_timezone.utc)
            jobs = jobs.filter(created_at__gte=since)
        jobs = list(jobs.order_by("created_at", "id"))
        ids = [job.pk for job in jobs]
        source_ids = [job.source_id for job in jobs]

        assessments = {}
        from automation.models import SiteAssessment
        for row in SiteAssessment.objects.filter(job_id__in=ids).order_by("job_id", "-generation", "-id"):
            assessments.setdefault(row.job_id, row)
        from automation.models import RecipeVersion
        best_scores = dict(RecipeVersion.objects.filter(job_id__in=ids).values("job_id")
                           .annotate(best=Max("score")).values_list("job_id", "best"))
        page_messages = defaultdict(list)
        requests = Counter()
        for job_id, status, message in ProbePage.objects.filter(job_id__in=ids).values_list("job_id", "status", "message"):
            if message:
                page_messages[job_id].append(message)
            if status in ("done", "failed"):
                requests[job_id] += 1
        leads_by_scope = defaultdict(Counter)
        for source_id, scope, n in (Observation.objects.filter(source_id__in=source_ids).order_by()
                                    .values("source_id", "lead__contact_scope").annotate(n=Count("lead", distinct=True))
                                    .values_list("source_id", "lead__contact_scope", "n")):
            leads_by_scope[source_id][scope] += n
        triggers = {}
        wanted = {job.original_url or job.source.url for job in jobs} | {job.source.url for job in jobs}
        for row in campaign.urls.filter(url__in=wanted).values("url", "label", "context", "score", "method"):
            triggers[row["url"]] = row

        rows, site_groups = [], defaultdict(list)
        for job in jobs:
            source = job.source
            authorized_url = job.original_url or source.url
            assessment = assessments.get(job.pk)
            messages = page_messages.get(job.pk, [])
            category, detail = stage(job, assessment, messages)
            target = next((t for t in (redirect_target(m) for m in [job.message] + messages) if t), "")
            redirect_class = (classify(authorized_url, target, allowed_paths=source.allowed_paths,
                                       allow_homepage=source.allow_homepage) if category == "redirect_scope" else "")
            if category == "redirect_scope" and detail == "redirect":
                detail = redirect_class
            accepted = job.last_good_recipe or job.current_recipe
            trigger = triggers.get(authorized_url, {})
            label = trigger.get("label", "")
            score_now = rank(campaign, authorized_url, label, trigger.get("context", ""))[0]
            admit_with_intent = bool(policy and score_now >= policy.min_url_score
                                     and direct_contact_page_intent(authorized_url, label)
                                     and in_scope(policy_scope(policy, authorized_url), authorized_url))
            site_groups[bare(origin(authorized_url).split("://", 1)[1])].append(job.pk)
            rows.append({
                "job_id": job.pk, "source_id": source.pk, "created_at": job.created_at, "state": job.state,
                "generation": job.generation, "approval_kind": source.approval_kind,
                "authorized_url": authorized_url, "current_origin": origin(source.url),
                "canonical_rebinds": job.canonical_rebinds,
                "category": category, "detail": detail,
                "redirect_target_origin": origin(target) if target and redirect_class not in ("invalid",) else "",
                "redirect_class": redirect_class,
                "assessment_outcome": assessment.outcome if assessment else "",
                "assessment_reasons": assessment.reasons if assessment else [],
                "best_recipe_score": best_scores.get(job.pk),
                "accepted_recipe_score": accepted.score if accepted else None,
                "accepted_below_model_floor": bool(accepted and accepted.score < MODEL_RECIPE_FLOOR),
                "observations_by_contact_scope": dict(leads_by_scope.get(source.pk, {})),
                "setup_requests": requests[job.pk],
                "trigger": {"method": trigger.get("method", ""), "stored_score": trigger.get("score"),
                            "score_now": score_now, "has_label": bool(label),
                            "direct_intent": direct_contact_page_intent(authorized_url, label)},
                "admitted_if_require_page_intent": admit_with_intent,
            })

        duplicates = {site: members for site, members in site_groups.items() if len(members) > 1}
        by_category = Counter(row["category"] for row in rows)
        by_detail = Counter(f"{row['category']}:{row['detail']}" for row in rows)
        rejected_by_intent = [row for row in rows if not row["admitted_if_require_page_intent"]]
        summary = {
            "campaign": {"id": campaign.pk, "name": campaign.name},
            "policy": ({"enabled": policy.enabled, "min_url_score": policy.min_url_score,
                        "min_recipe_score": policy.min_recipe_score, "model_recipe_floor": MODEL_RECIPE_FLOOR,
                        "require_page_intent": policy.require_page_intent,
                        "max_sites_per_day": policy.max_sites_per_day} if policy else None),
            "jobs": len(rows),
            "by_state": dict(Counter(row["state"] for row in rows)),
            "by_category": dict(by_category),
            "by_detail": dict(by_detail),
            "redirects": {
                "total": by_category["redirect_scope"],
                "safely_canonical_recoverable": sum(1 for r in rows if r["redirect_class"] == "canonical"),
                "canonical_path_out_of_scope": sum(1 for r in rows if r["redirect_class"] == "canonical_path_out_of_scope"),
                "rejected_other_subdomain": sum(1 for r in rows if r["redirect_class"] == "other_subdomain"),
                "rejected_cross_domain": sum(1 for r in rows if r["redirect_class"] == "cross_domain"),
                "rejected_downgrade_or_invalid": sum(1 for r in rows if r["redirect_class"] in ("downgrade", "invalid")),
                "unparsed_or_same_origin": sum(1 for r in rows if r["redirect_class"] in ("unparsed", "same_origin")),
                "already_rebound": sum(1 for r in rows if r["canonical_rebinds"]),
            },
            "same_site_duplicate_jobs": duplicates,
            "require_page_intent_replay": {
                "jobs_not_created": len(rejected_by_intent),
                "active_lost": sum(1 for r in rejected_by_intent if r["state"] == "active"),
                "failures_avoided_by_category": dict(Counter(r["category"] for r in rejected_by_intent
                                                             if r["state"] != "active")),
                "setup_requests_saved": sum(r["setup_requests"] for r in rejected_by_intent),
                "basis": "current rank() of the authorizing URL and label; min_url_score and allowed paths from the current policy",
            },
            "recipe_floor_replay": {
                "active_jobs": by_category["active"],
                "accepted_below_model_floor": sum(1 for r in rows if r["accepted_below_model_floor"]),
                "jobs": [r["job_id"] for r in rows if r["accepted_below_model_floor"]],
            },
            "leads_from_audited_sources": dict(sum((Counter(r["observations_by_contact_scope"]) for r in rows), Counter())),
            "generated_at": timezone.now(),
        }
        result = {"summary": summary} if options["no_jobs"] else {"summary": summary, "jobs": rows}
        self.stdout.write(json.dumps(result, cls=DjangoJSONEncoder, indent=2))
