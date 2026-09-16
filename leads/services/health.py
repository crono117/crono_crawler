"""Health derived from run history, including runs created before this feature."""
from django.db.models import OuterRef, Subquery


def recipe_review_runs(model, owner_field):
    # A successful refresh with existing contacts resolves the warning even when
    # it creates no new leads. Running/search-only/failed jobs are not recipe failures.
    latest = model.objects.filter(status="completed", **{owner_field: OuterRef(owner_field)}).order_by("-id")
    return model.objects.filter(pk=Subquery(latest.values("pk")[:1]), pages_done__gt=0,
                                contacts_seen=0).select_related(owner_field).order_by("-id")
