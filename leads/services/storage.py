import hashlib
from django.db import transaction
from django.utils import timezone
from leads.models import Lead, Observation

def identity(record, source, url):
    # Never merge people merely because they use the same business phone/email.
    name = record["name"].casefold()
    if record["email"] and record["contact_scope"] != "shared":
        key = [name, record["email"].casefold()]
    else:
        key = [name, record["company"].casefold(), str(source.pk), url]
    return hashlib.sha256("\x1f".join(key).encode()).hexdigest()

@transaction.atomic
def save_records(source, url, content_hash, records, company_tags):
    now = timezone.now()
    Observation.objects.filter(source=source, page_url=url).update(present=False)
    for record in records:
        facts = {k: record[k] for k in ("name", "company", "title", "email", "phone", "contact_scope")}
        lead, created = Lead.objects.get_or_create(identity=identity(record, source, url), defaults={**facts, "last_seen": now})
        if not created:
            # Preserve review decisions/notes; retain older non-empty fields as historical hints.
            for key, value in facts.items():
                if value:
                    setattr(lead, key, value)
            lead.last_seen = now
            lead.save()
        Observation.objects.update_or_create(lead=lead, source=source, page_url=url, defaults={
            "facts": facts, "source_category": source.category, "person_tags": record["person_tags"],
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
