"""Production lead inventory, independent of which worker lane saved a record."""
from django.db.models import Count, Exists, OuterRef, Q

from leads.models import Lead, Observation


def production_leads(campaign_id=None):
    """One row per stored identity with real source evidence; demo-only rows are excluded.

    Campaign attribution uses current source membership. It is not exclusive: a lead can
    have evidence on sources used by multiple campaigns, so campaign totals are not additive.
    """
    evidence = Observation.objects.filter(lead_id=OuterRef("pk")).exclude(source__collector="demo")
    if campaign_id is not None:
        evidence = evidence.filter(source__discovery_campaigns__pk=campaign_id)
    return Lead.objects.filter(Exists(evidence))


def production_totals(since, *, campaign_id=None):
    """Creation counts include every collection path and every review state, exactly once.

    Refreshing an observation does not make a lead new. Available/review counts retain
    their separate meaning; a suppression never disappears from creation history.
    """
    return production_leads(campaign_id).aggregate(
        total=Count("pk"),
        new=Count("pk", filter=Q(first_seen__gte=since)),
        shared=Count("pk", filter=Q(contact_scope="shared")),
        new_shared=Count("pk", filter=Q(first_seen__gte=since, contact_scope="shared")),
        available=Count("pk", filter=~Q(status__in=("rejected", "suppressed"))),
        needs_review=Count("pk", filter=Q(status="new")),
    )
