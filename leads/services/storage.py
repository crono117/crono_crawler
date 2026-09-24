import hashlib
import re
from collections import Counter
from django.db import transaction
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


@transaction.atomic
def save_records(source, url, content_hash, records, company_tags):
    now = timezone.now()
    Observation.objects.filter(source=source, page_url=url).update(present=False)
    names_on_page = Counter(record["name"].casefold() for record in records)
    for record in records:
        facts = {k: record[k] for k in LEAD_FIELDS}
        key = identity(record, source, url)
        lead = Lead.objects.select_for_update().filter(identity=key).first()
        previous = None
        if key != page_identity(record, source, url):
            previous = Lead.objects.select_for_update().filter(identity=page_identity(record, source, url)).first()
        if lead is None and previous and names_on_page[record["name"].casefold()] == 1 and continuous(previous, record):
            # The same card gained an email: keep the stored lead, its review state and notes.
            previous.identity = key
            lead = previous
        created = lead is None
        if created:
            lead = Lead(identity=key, **facts, last_seen=now)
        if (previous and previous.pk != lead.pk and previous.status in HELD_STATUSES and lead.status == "new"
                and f"lead #{previous.pk} " not in lead.notes):
            # An unmatched successor must not become an exportable replacement for a held record.
            lead.status = previous.status
            lead.notes = "\n".join(filter(None, [lead.notes, hold_note(previous, url)]))
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
