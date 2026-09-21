"""Deliberately fictional, fixed HTML. Never resolve these domains."""
from pathlib import Path
from django.conf import settings
from leads.services.network import Response

DEMO_ORIGIN = 'https://jev.example.test'


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
