"""Deliberately fictional, fixed HTML. Never resolve these domains."""
from pathlib import Path
from django.conf import settings
from leads.services.network import Response

DEMO_ORIGIN = 'https://jev.example.test'
CALIBRATION_URL = 'https://jev-calibration.example.test/'
CALIBRATION_HTML = '''<h1>Fictional Aster Calibration</h1>
<p>Fictional Aster Calibration develops software. This is synthetic test evidence.</p>
<article class="team-member"><h2 class="name">Jordan Fictional</h2>
<p class="title">Software Engineer</p><p class="company">Fictional Aster Calibration</p></article>'''
CALIBRATION_SOURCE = {
    'name': 'Fictional Jev calibration (no website)', 'company': 'Fictional Aster Calibration',
    'collector': 'demo', 'approved': True, 'active': False, 'allowed_paths': '/fixture-only',
    'allow_homepage': True, 'follow_links': False, 'recipe': {}, 'setup_mode': 'rules_only',
    'approval_kind': 'operator',
    'approval_notes': 'Only the packaged fictional calibration text is authorized. No real website or contact evidence.'}


def calibration_unchanged(source):
    return (source.url == CALIBRATION_URL and
            all(getattr(source, key) == value for key, value in CALIBRATION_SOURCE.items()) and
            not source.discovery_campaigns.exists())


def seed_calibration(provider='mock'):
    """Prepare fixed fictional evidence only; no campaign, pilot, fetch or dispatch."""
    import hashlib
    from django.db import transaction
    from leads.models import Source
    from .evidence import capture_page
    from .models import EvidenceDocument
    with transaction.atomic():
        previous = EvidenceDocument.objects.filter(provenance='synthetic-fixture').select_related('source').first()
        if previous and not calibration_unchanged(previous.source):
            raise ValueError('Calibration source changed; existing evidence and permissions are preserved.')
        source, _ = Source.objects.get_or_create(url=CALIBRATION_URL, defaults=CALIBRATION_SOURCE)
        if not calibration_unchanged(source):
            raise ValueError('Calibration source changed; use an isolated database or review it without reviving permissions.')
        return capture_page(source, source.url, hashlib.sha256(CALIBRATION_HTML.encode()).hexdigest(),
                            CALIBRATION_HTML, provider=provider, synthetic=True)


def demo_response(url):
    names = {DEMO_ORIGIN + '/': 'company.html', DEMO_ORIGIN + '/team/': 'team.html'}
    name = names.get(url)
    body = (Path(settings.BASE_DIR) / 'examples' / 'jev' / name).read_bytes() if name else b'Not in offline fixture'
    return Response(url, 200 if name else 404, {'content-type': 'text/html'}, body)


def seed_demo():
    import hashlib
    from datetime import timedelta
    from django.utils import timezone
    from leads.models import Source
    from discovery.models import Campaign
    from .evidence import capture_page
    from .models import CompanyDomain, Pilot
    from .routing import verify_domain
    source, _ = Source.objects.get_or_create(url=DEMO_ORIGIN + '/', defaults={
        'name': 'Jev offline demo', 'company': 'Aster Systems', 'collector': 'demo',
        'approved': True, 'allowed_paths': '/team', 'allow_homepage': True, 'delay_seconds': 2,
        'max_pages': 10, 'max_depth': 2, 'follow_links': False})
    if source.collector != 'demo' or not source.approved:
        raise ValueError('Existing demo source was changed; use a fresh isolated DATA_DIR.')
    campaign, _ = Campaign.objects.get_or_create(name='Jev offline demo', defaults={
        'active': True, 'use_sitemaps': False, 'next_due_at': timezone.now() + timedelta(days=7),
        'keywords': 'software\npayment processing\npoint of sale', 'min_score': 0})
    campaign.sources.add(source)
    pilot, _ = Pilot.objects.get_or_create(name='Jev offline demo', campaign=campaign, defaults={
        'active': True, 'expires_at': timezone.now() + timedelta(days=1)})
    response = demo_response(source.url)
    evaluations = capture_page(source, source.url, hashlib.sha256(response.body).hexdigest(), response.text, provider='mock')
    for domain in CompanyDomain.objects.filter(span__document__source=source):
        if domain.state != 'verified':
            verify_domain(domain.pk, 'offline demo', 'Fixed fictional fixture explicitly identifies this company and domain.')
    return pilot, evaluations
