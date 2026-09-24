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
from leads.services.extraction import (DEFAULT_SELECTORS, EMAIL, GENERIC, PHONE, contact_evidence,
                                       normalize, selected_text, signature, soup_for)
from leads.services.network import in_scope, origin
from discovery.ranking import clean_url
from leads.services.structured import entities
from .contracts import digest
from .models import (Affiliation, Company, CompanyDomain, ContactCandidate, EvidenceDocument,
                     EvidenceSpan, Person)

VERSION = 'candidate-v1.0'
LAYERED_VERSION = 'blocks-v1'
ROLE = re.compile(r'\b(?:chief|president|vice president|vp|director|manager|sales|engineer|founder|owner|partner|consultant|agent|advisor|broker|specialist|representative|account executive|business development)\b', re.I)
BLOCK_TAGS = ('article', 'section', 'li', 'div', 'address')
EXCLUDED_TAGS = {'header', 'nav', 'footer', 'form', 'dialog', 'template'}
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


def add_span_at(doc, key, start, end, kind, locator=''):
    if not 0 <= start < end <= len(doc.text):
        return None
    text = doc.text[start:end]
    span, _ = EvidenceSpan.objects.get_or_create(document=doc, key=key, defaults={
        'start': start, 'end': end, 'text_hash': digest(text), 'kind': kind, 'locator': locator[:200]})
    return span


def _hidden_or_excluded(node):
    for current in (node, *node.parents):
        if getattr(current, 'name', None) in EXCLUDED_TAGS:
            return True
        attrs = getattr(current, 'attrs', {}) or {}
        classes = {str(value).casefold() for value in attrs.get('class', [])}
        style = str(attrs.get('style', '')).replace(' ', '').casefold()
        if ('hidden' in attrs or str(attrs.get('aria-hidden', '')).casefold() == 'true' or
                classes.intersection({'hidden', 'visually-hidden', 'sr-only'}) or
                'display:none' in style or 'visibility:hidden' in style):
            return True
    return False


def _bounded_block_text(node, text, name, limit):
    if len(text) <= limit:
        return text
    role = ROLE.search(text)
    contact = EMAIL.search(text) or PHONE.search(text)
    if not role or not contact:
        return ''
    role_text = text[max(0, role.start() - 80):min(len(text), role.end() + 160)].strip()
    contact_text = contact.group(0)
    prefix = normalize(f'{name} {role_text}')
    available = limit - len(contact_text) - 1
    if available < len(name):
        return ''
    return normalize(f'{prefix[:available].rstrip()} {contact_text}')


def candidate_blocks(soup, covered=(), covered_names=()):
    """Bounded generic person blocks not already captured by a CSS row or structured Person.

    A block that is, contains, or sits inside a covered node, or whose own heading
    names exactly a covered person, would duplicate an existing candidate and is
    skipped. Names mentioned elsewhere in a block (a colleague, or a longer name that
    contains a covered one) do not make it covered.
    """
    covered_ids = set()
    for node in covered:
        covered_ids.add(id(node))
        covered_ids.update(id(parent) for parent in node.parents)
        covered_ids.update(id(child) for child in node.find_all(True))
    names = {normalize(name).casefold() for name in covered_names if name}
    candidates = []
    for node in soup.find_all(BLOCK_TAGS, limit=2000):
        if _hidden_or_excluded(node):
            continue
        text = contact_evidence(node)
        if not text or len(text) < 12:
            continue
        heading = node.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        name = normalize(heading.get_text(' ', strip=True)) if heading else ''
        words = name.split()
        plausible_name = (2 <= len(words) <= 8 and len(name) <= 100 and '@' not in name and
                          name.casefold() not in {'contact us', 'our team', 'leadership team', 'meet the team'})
        has_contact = bool(node.select_one('a[href^="mailto:"], a[href^="tel:"]') or EMAIL.search(text) or PHONE.search(text))
        if plausible_name and ROLE.search(text) and has_contact:
            candidates.append((node, text, name))
    qualifying = {id(node) for node, _, _ in candidates}
    blocks, seen, aggregate = [], set(), 0
    for node, text, name in candidates:
        if any(id(descendant) in qualifying for descendant in node.find_all(BLOCK_TAGS)):
            continue
        canonical = text.casefold()
        if canonical in seen or id(node) in covered_ids or name.casefold() in names:
            continue
        remaining = 2400 - aggregate
        if remaining <= 0 or len(blocks) == 6:
            break
        bounded = _bounded_block_text(node, text, name, min(500, remaining))
        if not bounded:
            continue
        seen.add(canonical)
        blocks.append({'id': f'b{len(blocks) + 1}', 'text': bounded})
        aggregate += len(bounded)
    return blocks


def project_blocks(text, blocks):
    offsets = []
    for block in blocks:
        text += f"\n[Candidate block {block['id']}]\n"
        start = len(text)
        text += block['text']
        offsets.append((start, len(text)))
        text += f"\n[/Candidate block {block['id']}]"
    return text, offsets


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


def usable_rows(rows, selectors):
    """CSS rows that name one plausible person with bounded card evidence."""
    usable = []
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
        usable.append((index, row, name, person_text))
    return usable


def company_passage(visible_text, company_name, limit=1200):
    """Bounded page text that actually contains the company name.

    The page opening is kept when it already names the company, so existing
    evidence spans are unchanged; otherwise the window starts shortly before the
    first mention. An absent name yields the opening, which the caller rejects.
    """
    opening = visible_text[:limit]
    position = visible_text.casefold().find(company_name.casefold()) if company_name else -1
    if position < 0 or position + len(company_name) <= limit:
        return 'company', opening, 'page-opening'
    start = max(0, position - 200)
    return 'company-mention', visible_text[start:start + limit], 'company-mention'


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
    selector = source.recipe.get('row') or DEFAULT_SELECTORS['row']
    selectors = DEFAULT_SELECTORS | (source.recipe or {})
    rows = [] if source.recipe.get('engine') else soup.select(selector, limit=100)
    person_rows = usable_rows(rows, selectors)
    # Only an employer-backed structured Person is captured below; a bare author is not coverage.
    structured_people = [item['fields']['name'] for item in structured_items
                         if item['kind'] == 'Person' and item['fields']['company']]
    layered_blocks = []
    if settings.JEV_CAPTURE_ENABLED and settings.JEV_LAYERED_BLOCKS_ENABLED and not company_job:
        layered_blocks = candidate_blocks(soup, covered=[row for _, row, _, _ in person_rows],
                                          covered_names=[name for _, _, name, _ in person_rows] + structured_people)
    text = visible_text
    if structured_items:
        text += "\n[Parsed schema.org direct properties; canonical projection]\n"
        text += "\n".join(item['evidence'] for item in structured_items)
    block_offsets = []
    original_length = len(text)
    projection = ''
    if layered_blocks:
        projection, relative_offsets = project_blocks('', layered_blocks)
        base = text[:max(0, 50000 - len(projection))]
        shift = len(base)
        text = base + projection
        block_offsets = [(start + shift, end + shift) for start, end in relative_offsets]
    truncated = original_length + (len(projection) if layered_blocks else 0) > 50000 or structured_stats['limit_reached']
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
    fingerprint_parts = [source.pk, url, body_hash, sig, extraction_sig, VERSION]
    if layered_blocks:
        fingerprint_parts.append(LAYERED_VERSION)
    fingerprint = digest(fingerprint_parts)
    provenance = ('synthetic-fixture' if synthetic else 'offline-demo' if source.collector == 'demo'
                  else 'fetched+structured-v1' if structured_items else
                  f'fetched+{LAYERED_VERSION}' if layered_blocks else 'fetched')
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
    company_key, company_text, company_locator = company_passage(visible_text, company_name)
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
    company_span = add_span(doc, company_key, company_text, 'company', company_locator)
    if not company_span or company_name.casefold() not in company_text.casefold():
        return []
    # A reviewed source is not automatic proof of company-domain ownership.
    CompanyDomain.objects.get_or_create(company=company, url=source.url, defaults={
        'origin': origin(source.url), 'span': company_span})
    from .services import enqueue_subject
    evaluations = enqueue_subject(doc, company, company_span, provider=provider, company_job=company_job)
    if layered_blocks:
        from .services import enqueue_page_blocks
        saved_blocks = []
        for block, (start, end) in zip(layered_blocks, block_offsets):
            span = add_span_at(doc, f"candidate-block-{block['id']}", start, end,
                               'candidate_block', block['id'])
            if span:
                saved_blocks.append({'id': block['id'], 'span': span})
        if saved_blocks:
            evaluations += enqueue_page_blocks(doc, company, company_span, saved_blocks, provider=provider)
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
    for index, row, name, person_text in person_rows:
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
