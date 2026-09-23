"""Bounded, metadata-only operator import. No DNS, HTTP or queue side effects."""
import json
import unicodedata
from urllib.parse import urlsplit, unquote

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction

from automation.models import SitePolicy
from automation.policy import denied
from leads.services.network import origin
from .models import Campaign, DiscoveredURL
from .ranking import clean_url, rank

MAX_BATCH = 10
MAX_BYTES = 16 * 1024


def metadata_url(raw):
    # Validate raw input before parsers strip whitespace or normalization hides it.
    if not isinstance(raw, str) or len(raw) > 1500 or raw != raw.strip():
        raise ValueError('Invalid metadata URL.')
    if any(unicodedata.category(c).startswith('C') for c in unquote(raw)):
        raise ValueError('Invalid metadata URL characters.')
    parsed = urlsplit(raw)
    if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None:
        raise ValueError('Invalid metadata URL scheme/credentials.')
    URLValidator(schemes=['http', 'https'])(raw)
    return clean_url(raw)


def parse_metadata(body, limit=MAX_BATCH):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError('Batch limit must be 1–10.')
    if len(body) > MAX_BYTES:
        raise ValueError('Metadata file exceeds 16 KiB.')
    try:
        rows = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Use a UTF-8 JSON array of URL metadata objects.') from None
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise ValueError(f'Supply 1–{limit} metadata objects.')
    validated = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - {'url', 'label', 'context'} or 'url' not in row:
            raise ValueError(f'Row {index}: only url, label and context are accepted; url is required.')
        for key, maximum in (('url', 1500), ('label', 200), ('context', 600)):
            value = row.get(key, '')
            if not isinstance(value, str) or len(value) > maximum or any(unicodedata.category(c).startswith('C') for c in value):
                raise ValueError(f'Row {index}: invalid {key} type, length or control characters.')
        try:
            url = metadata_url(row['url'])
        except (ValueError, UnicodeError, ValidationError):
            # Do not echo a rejected URL: it might contain credentials.
            raise ValueError(f'Row {index}: use a public HTTP(S) content URL with no credentials or local destination.') from None
        validated.append({'url': url, 'label': row.get('label', ''), 'context': row.get('context', '')})
    return validated


@transaction.atomic
def import_metadata(campaign_id, rows, *, apply=False):
    # Enforce the same bounded contract for direct callers, before any mutation.
    rows = parse_metadata(json.dumps(rows, ensure_ascii=False).encode('utf-8'))
    # The lock serializes imports on PostgreSQL; SQLite uses BEGIN IMMEDIATE.
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
    policy = SitePolicy.objects.filter(campaign=campaign).first()
    count = campaign.urls.count()
    seen, results = set(), []
    for row in rows:
        url = row['url']
        target_origin = origin(url)
        existing = campaign.urls.filter(url=url).first()
        score, reasons = rank(campaign, url, row['label'], row['context'])
        if url in seen:
            outcome = 'duplicate_in_batch'
        elif existing:
            outcome = 'existing_' + existing.decision
            # Never revive dismissals, replace evidence/context, attach a source,
            # or alter approval. Pending imports require an explicit review.
            if apply and existing.decision == 'pending' and not existing.manual_review_required:
                existing.manual_review_required = True
                existing.save(update_fields=['manual_review_required'])
        elif campaign.urls.filter(origin=target_origin, dismissal_scope='origin').exists():
            outcome = 'dismissed_origin'
        elif score < 0 or (policy and denied(policy, url)):
            outcome = 'excluded'
        elif count >= campaign.max_candidates:
            outcome = 'inventory_full'
        else:
            outcome = 'created' if apply else 'would_create'
            count += 1
            if apply:
                DiscoveredURL.objects.create(campaign=campaign, url=url, origin=target_origin,
                    label=row['label'], context=row['context'], method='operator_import',
                    score=score, reasons=reasons + ['Operator metadata import; manual review required.'],
                    manual_review_required=True, decision='pending')
        seen.add(url)
        results.append({'row': len(results) + 1, 'outcome': outcome})
    return {'campaign': campaign.pk, 'dry_run': not apply, 'max_candidates': campaign.max_candidates,
            'inventory_after': count, 'network_requests': 0, 'jobs_created': 0,
            'hostname_publicness': 'unresolved; checked only by authorized transport', 'results': results}
