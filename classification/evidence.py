"""Capture candidates before the existing accepted-contact filter, without I/O."""
import logging
import hashlib
import re
from datetime import timedelta
from urllib.parse import urljoin
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from automation.policy import scope_hash
from leads.services.extraction import (DEFAULT_SELECTORS, EMAIL, GENERIC, contact_evidence,
                                       normalize, selected_text, signature, soup_for)
from leads.services.network import in_scope, origin
from discovery.ranking import clean_url
from leads.services.structured import entities
from .contracts import digest
from .models import (Affiliation, Company, CompanyDomain, ContactCandidate, EvidenceDocument,
                     EvidenceSpan, Person)

VERSION = 'candidate-v1.0'
PHONE = re.compile(r'(?<!\w)(?:\+\d[\d ().-]{6,20}\d|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})(?!\w)')
logger = logging.getLogger(__name__)


def source_authorized(source, url):
    if not source.approved or not in_scope(source, url):
        return False
    if source.setup_mode == 'automatic' or source.approval_kind == 'policy':
        from automation.models import SiteAutomationJob
        from automation.policy import authorized
        job = SiteAutomationJob.objects.select_related('source', 'campaign').filter(source=source).first()
        return bool(job and authorized(job))
    return True


def valid_span(span):
    doc = span.document
    return bool(doc.text and 0 <= span.start < span.end <= len(doc.text) and
                digest(span.text) == span.text_hash and digest(doc.text) == doc.text_hash and
                (not doc.expires_at or doc.expires_at > timezone.now()))


def add_span(doc, key, text, kind, locator=''):
    start = doc.text.find(text)
    if start < 0 or not text:
        return None
    span, _ = EvidenceSpan.objects.get_or_create(document=doc, key=key, defaults={
        'start': start, 'end': start + len(text), 'text_hash': digest(text), 'kind': kind, 'locator': locator[:200]})
    return span


def contacts_for(span, company, person=None, shared=False):
    values = [('email', v) for v in EMAIL.findall(span.text)] + [('phone', v) for v in PHONE.findall(span.text)]
    contacts = []
    for kind, value in list(dict.fromkeys(values))[:10]:
        shared_hint = shared or (kind == 'email' and value.split('@')[0].lower() in GENERIC)
        item, _ = ContactCandidate.objects.get_or_create(span=span, kind=kind, value=value[:254], defaults={
            'normalized': value.lower() if kind == 'email' else re.sub(r'\D', '', value),
            'company': company, 'person': None if shared_hint else person, 'shared_hint': shared_hint,
            'association': 'company_shared' if shared_hint else 'unknown'})
        contacts.append(item)
    return contacts


@transaction.atomic
def capture_page(source, url, body_hash, html, *, provider=None, company_job=None, retrieved_at=None, synthetic=False):
    """Caller supplies already-authorized HTML; this function never fetches anything."""
    source.refresh_from_db()
    if synthetic:
        from .fixtures import CALIBRATION_URL, CALIBRATION_HTML, calibration_unchanged
        if (not calibration_unchanged(source) or url != CALIBRATION_URL or company_job or retrieved_at or
                html != CALIBRATION_HTML or body_hash != hashlib.sha256(CALIBRATION_HTML.encode()).hexdigest()):
            raise ValueError('Synthetic calibration must use the fixed offline fixture without retrieval or company work.')
    if not source_authorized(source, url):
        return []
    if company_job and company_job.source_id != source.pk:
        return []
    provider = provider or ('live' if settings.JEV_MODE == 'live' else 'mock')
    soup = soup_for(html)
    visible_text = contact_evidence(soup)
    structured_items, structured_stats = entities(html)
    text = visible_text
    if structured_items:
        text += "\n[Parsed schema.org direct properties; canonical projection]\n"
        text += "\n".join(item['evidence'] for item in structured_items)
    truncated = len(text) > 50000 or structured_stats['limit_reached']
    text = text[:50000]
    if not text:
        return []
    now = retrieved_at or timezone.now()
    sig = scope_hash(source)
    extraction_sig = signature(source)
    links = []
    for node in soup.select('a[href]')[:2000]:
        try:
            target = clean_url(urljoin(url, node['href']))
        except (ValueError, UnicodeError):
            continue
        if target not in {link['url'] for link in links}:
            links.append({'url': target, 'label': normalize(node.get_text(' ', strip=True))[:160]})
        if len(links) == 100:
            break
    fingerprint = digest([source.pk, url, body_hash, sig, extraction_sig, VERSION])
    provenance = ('synthetic-fixture' if synthetic else 'offline-demo' if source.collector == 'demo'
                  else 'fetched+structured-v1' if structured_items else 'fetched')
    if synthetic:
        fingerprint = digest([fingerprint, 'synthetic-fixture'])
    previous = EvidenceDocument.objects.filter(fingerprint=fingerprint).first()
    if previous and (not previous.text or (previous.expires_at and previous.expires_at <= now)):
        # Recapturing unchanged HTML after retention expiry creates a new immutable
        # version; subsequent captures reuse that retained version rather than revive old evidence.
        retained = EvidenceDocument.objects.filter(source=source, url=url, content_hash=body_hash,
            scope_hash=sig, extraction_signature=extraction_sig, parser_version=VERSION,
            provenance=provenance, expires_at__gt=now).exclude(text='').order_by('-retrieved_at').first()
        fingerprint = retained.fingerprint if retained else digest([fingerprint, now.isoformat()])
    doc, created = EvidenceDocument.objects.get_or_create(
        fingerprint=fingerprint, defaults={
            'source': source, 'url': url, 'retrieved_at': None if synthetic else now, 'last_checked': now, 'content_hash': body_hash,
            'scope_hash': sig, 'parser_version': VERSION, 'extraction_signature': extraction_sig,
            'text': text, 'text_hash': digest(text), 'links': links,
            'provenance': provenance,
            'truncated': truncated, 'expires_at': now + timedelta(days=30)})
    if not created:
        # Never overwrite original evidence or original retrieval time.
        EvidenceDocument.objects.filter(pk=doc.pk).update(last_checked=now)
        doc.last_checked = now
        if not doc.text or (doc.expires_at and doc.expires_at <= now):
            return []
    title_node = soup.select_one('[itemtype*="Organization"] [itemprop="name"], .company-name, h1')
    title = normalize(title_node.get_text(' ', strip=True)) if title_node else ''
    company_name = source.company if source.company and source.company.casefold() in visible_text.casefold() else title
    company_text = visible_text[:1200]
    company_locator = 'page-opening'
    if not company_name or company_name.lower() in ('our team', 'team', 'contact us', 'about us', 'home'):
        organizations = [item for item in structured_items if item['kind'] == 'Organization']
        # Multiple organizations do not establish which one owns the page.
        if len(organizations) == 1:
            company_name = organizations[0]['fields']['name']
            company_text = organizations[0]['evidence']
            company_locator = organizations[0]['locator']
    if not company_name or len(company_name) > 200 or company_name.lower() in ('our team', 'team', 'contact us', 'about us', 'home'):
        return []
    company, _ = Company.objects.get_or_create(identity=digest([origin(url), company_name.casefold()]), defaults={'name': company_name})
    company_span = add_span(doc, 'company', company_text, 'company', company_locator)
    if not company_span or company_name.casefold() not in company_text.casefold():
        return []
    # A reviewed source is not automatic proof of company-domain ownership.
    CompanyDomain.objects.get_or_create(company=company, url=source.url, defaults={
        'origin': origin(source.url), 'span': company_span})
    from .services import enqueue_subject
    evaluations = enqueue_subject(doc, company, company_span, provider=provider, company_job=company_job)
    for index, item in enumerate(structured_items):
        fields = item['fields']
        # An unrelated author or a bare Person does not establish employment.
        if item['kind'] != 'Person' or not fields['company']:
            continue
        span = add_span(doc, f'structured-person-{index}', item['evidence'], 'person', item['locator'])
        if not span:
            continue
        person, _ = Person.objects.get_or_create(identity=digest([fields['name'].casefold(), source.pk, url]),
                                                 defaults={'name': fields['name']})
        employer = company
        if fields['company'].casefold() != company.name.casefold():
            employer, _ = Company.objects.get_or_create(identity=digest([origin(url), fields['company'].casefold()]),
                                                       defaults={'name': fields['company']})
            evaluations += enqueue_subject(doc, employer, span, provider=provider, company_job=company_job)
        Affiliation.objects.get_or_create(person=person, company=employer, span=span, defaults={'title': fields['title']})
        contacts = contacts_for(span, employer, person)
        evaluations += enqueue_subject(doc, employer, span, person=person, contacts=contacts[:2],
                                       provider=provider, company_job=company_job)
    selector = source.recipe.get('row') or DEFAULT_SELECTORS['row']
    selectors = DEFAULT_SELECTORS | (source.recipe or {})
    rows = [] if source.recipe.get('engine') else soup.select(selector, limit=100)
    for index, row in enumerate(rows):
        if settings.EXTRACTION_PACKS_ENABLED and (row.get('itemtype', '').rstrip('/').endswith('/Person') or
                row.find_parent(attrs={'itemscope': True})):
            # Structured adapter owns item-scope associations when enabled.
            continue
        name = selected_text(row, selectors['name'])
        if not 2 <= len(name.split()) <= 8 or len(name) > 100 or '@' in name:
            continue
        person_text = contact_evidence(row)
        if not person_text or len(person_text) > 1200 or name.casefold() not in person_text.casefold():
            continue
        span = add_span(doc, f'person-{index}', person_text, 'person', selector)
        if not span:
            continue
        person, _ = Person.objects.get_or_create(identity=digest([name.casefold(), source.pk, url]), defaults={'name': name})
        title = selected_text(row, selectors['title'])[:200]
        if title.casefold() not in person_text.casefold():
            title = ''
        row_company = selected_text(row, selectors['company'])[:200]
        employer = company
        if row_company and row_company.casefold() in person_text.casefold() and row_company.casefold() != company.name.casefold():
            employer, _ = Company.objects.get_or_create(identity=digest([origin(url), row_company.casefold()]), defaults={'name': row_company})
            evaluations += enqueue_subject(doc, employer, span, provider=provider, company_job=company_job)
        Affiliation.objects.get_or_create(person=person, company=employer, span=span, defaults={'title': title})
        contacts = contacts_for(span, employer, person)
        # An explicit website link in this person's block is only a domain candidate.
        for node in row.select('a.website[href], a[itemprop="url"][href]')[:3]:
            try:
                target = clean_url(urljoin(url, node['href']))
            except (ValueError, UnicodeError):
                continue
            CompanyDomain.objects.get_or_create(company=employer, url=target, defaults={'origin': origin(target), 'span': span})
        evaluations += enqueue_subject(doc, employer, span, person=person, contacts=contacts[:2], provider=provider, company_job=company_job)
    for index, row in enumerate(soup.select('footer, [role="contentinfo"]')[:5]):
        footer = contact_evidence(row)
        if len(footer) <= 1200:
            span = add_span(doc, f'footer-{index}', footer, 'shared', 'footer')
            if span:
                contacts_for(span, company, shared=True)
    return evaluations


def capture_if_enabled(source, response, company_job=None):
    if not settings.JEV_CAPTURE_ENABLED:
        return
    try:
        capture_page(source, response.url, hashlib.sha256(response.body).hexdigest(), response.text, company_job=company_job)
    except Exception as exc:
        # New optional capture cannot make accepted legacy contacts disappear.
        logger.warning('Candidate capture for source %s failed (%s).', source.pk, type(exc).__name__)


def purge_expired():
    now = timezone.now()
    ids = list(EvidenceDocument.objects.filter(expires_at__lte=now).exclude(text='').values_list('pk', flat=True)[:100])
    if ids:
        from .models import ContactCandidate, Evaluation
        # Remove unpromoted candidate values with their expired source text. Money remains.
        ContactCandidate.objects.filter(span__document_id__in=ids).delete()
        EvidenceDocument.objects.filter(pk__in=ids).update(text='', links=[])
        Evaluation.objects.filter(document_id__in=ids).update(request={}, state='evidence_expired', reason='Candidate evidence retention expired.')
