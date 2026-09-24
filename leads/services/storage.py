import hashlib
import re
from collections import Counter
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from leads.models import Lead, Observation

LEAD_FIELDS = ("name", "company", "title", "email", "phone", "contact_scope")
HELD_STATUSES = ("suppressed", "rejected")


def page_identity(record, source, url):
    return hashlib.sha256("\x1f".join(
        [record["name"].casefold(), record["company"].casefold(), str(source.pk), url]).encode()).hexdigest()


def identity(record, source, url):
    # Never merge people merely because they use the same business phone/email.
    if record["email"] and record["contact_scope"] != "shared":
        return hashlib.sha256("\x1f".join([record["name"].casefold(), record["email"].casefold()]).encode()).hexdigest()
    return page_identity(record, source, url)


def _digits(value):
    return re.sub(r"\D", "", value or "")


def continuous(previous, record):
    """The page-scoped lead is provably this record before it gained an email.

    Same name, company, source and page are implied by the identity. Its stored
    contacts must not conflict: no different email, and no different phone.
    """
    if previous.email and previous.email.casefold() != record["email"].casefold():
        return False
    return not (previous.phone and record["phone"] and _digits(previous.phone) != _digits(record["phone"]))


def hold_note(previous, url):
    return (f"Held as {previous.get_status_display()}: lead #{previous.pk} for this name on {url} was "
            f"{previous.get_status_display().lower()} and its identity could not be matched automatically. "
            "Review both records before changing this status.")


def holdable(lead):
    """Only a record still at its default status can inherit a hold; operator decisions stand."""
    return lead.status == "new" and lead.status_origin != "operator"


def hold(lead, held, url):
    lead.status, lead.status_origin, lead.held_by = held.status, "hold", held
    lead.notes = "\n".join(filter(None, [lead.notes, hold_note(held, url)]))


def page_namesakes(lead):
    """Other leads with this name observed on a source page where `lead` was observed, each with that page."""
    pages = set(Observation.objects.filter(lead=lead).values_list("source_id", "page_url"))
    if not pages:
        return []
    shared = Q()
    for source_id, page_url in pages:
        shared |= Q(source_id=source_id, page_url=page_url)
    first_page = {}
    for lead_id, page_url in Observation.objects.filter(shared).exclude(lead=lead).order_by("pk").values_list(
            "lead_id", "page_url"):
        first_page.setdefault(lead_id, page_url)
    others = Lead.objects.select_for_update().filter(pk__in=first_page).order_by("pk")
    return [(other, first_page[other.pk]) for other in others if other.name.casefold() == lead.name.casefold()]


@transaction.atomic
def record_review(lead, status_changed):
    """Save an operator review. A suppression/rejection protects unresolved namesakes at once."""
    if status_changed:
        lead.status_origin, lead.held_by, lead.status_decided_at = "operator", None, timezone.now()
    lead.save()
    held = 0
    if lead.status in HELD_STATUSES:
        for other, url in page_namesakes(lead):
            if holdable(other):
                hold(other, lead, url)
                other.save(update_fields=["status", "status_origin", "held_by", "notes"])
                held += 1
    return held


def unique_card(record, records_by_name):
    """Extraction saw exactly one selector-matched card with this name, before any filtering.

    Records without that signal (local AI, structured recipes) never prove continuity.
    """
    return record.get("name_cards") == 1 and records_by_name[record["name"].casefold()] == 1


@transaction.atomic
def save_records(source, url, content_hash, records, company_tags):
    now = timezone.now()
    observations = Observation.objects.filter(source=source, page_url=url)
    # Observation history outlives identity changes, so it links a reviewed person on this
    # page to later records whose email was added, lost, changed or became ambiguous.
    observed_ids = set(observations.values_list("lead_id", flat=True))
    page_history = list(Lead.objects.filter(pk__in=observed_ids).order_by("pk"))
    observations.update(present=False)
    records_by_name = Counter(record["name"].casefold() for record in records)
    for record in records:
        facts = {k: record[k] for k in LEAD_FIELDS}
        key = identity(record, source, url)
        lead = Lead.objects.select_for_update().filter(identity=key).first()
        previous = None
        if key != page_identity(record, source, url):
            previous = Lead.objects.select_for_update().filter(identity=page_identity(record, source, url)).first()
        if lead is None and previous and unique_card(record, records_by_name) and continuous(previous, record):
            # The same card gained an email: keep the stored lead, its review state and notes.
            previous.identity = key
            lead = previous
        created = lead is None
        if created:
            lead = Lead(identity=key, **facts, last_seen=now)
        held = [other for other in page_history if other.status in HELD_STATUSES and other.pk != lead.pk
                and other.name.casefold() == record["name"].casefold()]
        if held and holdable(lead):
            # A same-name record on this page must not become an exportable replacement for a
            # held lead; an operator decides whether they are the same person.
            hold(lead, held[0], url)
        if not created:
            # Preserve review decisions/notes; retain older non-empty fields as historical hints.
            for field, value in facts.items():
                if value:
                    setattr(lead, field, value)
            lead.last_seen = now
        lead.save()
        Observation.objects.update_or_create(lead=lead, source=source, page_url=url, defaults={
            "facts": {**facts, "contact_provenance": record.get("contact_provenance", {})},
            "source_category": source.category, "person_tags": record["person_tags"],
            "company_tags": company_tags, "evidence": record["evidence"], "method": source.extractor,
            "content_hash": content_hash, "present": True, "last_seen": now,
        })
    return len(records)

@transaction.atomic
def touch_unchanged(source, url):
    now = timezone.now()
    observations = Observation.objects.filter(source=source, page_url=url, present=True)
    ids = list(observations.values_list("lead_id", flat=True))
    observations.update(last_seen=now)
    Lead.objects.filter(pk__in=ids).update(last_seen=now)
    return len(ids)
