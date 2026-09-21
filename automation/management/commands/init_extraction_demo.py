from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from automation.fixtures import DEMO_ORIGIN
from automation.models import SitePolicy
from automation.services import start_setup
from discovery.models import Campaign
from leads.models import Source


class Command(BaseCommand):
    help = 'Queue five fixed synthetic extraction layouts through the existing worker and canary gates.'

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.EXTRACTION_PACKS_ENABLED:
            raise CommandError('Set EXTRACTION_PACKS_ENABLED=1 in the web and worker environment first.')
        if settings.JEV_MODE == 'live':
            raise CommandError('Use JEV_MODE=mock or off for the fictional extraction demo.')
        campaign, _ = Campaign.objects.get_or_create(name='Offline extraction packs demo', defaults={
            'active': True, 'use_sitemaps': False, 'min_score': 0,
            'next_due_at': timezone.now() + timedelta(days=7)})
        SitePolicy.objects.get_or_create(campaign=campaign, defaults={'enabled': True, 'probe_pages': 1, 'canary_pages': 1})
        for name in ('wordpress', 'webflow', 'squarespace', 'jsonld', 'microdata'):
            path = f'/extraction/{name}/'
            source, _ = Source.objects.get_or_create(url=DEMO_ORIGIN + path, defaults={
                'name': f'Fictional {name} layout', 'company': 'Example Payments',
                'approved': True, 'approval_kind': 'policy', 'setup_mode': 'automatic',
                'collector': 'demo', 'allowed_paths': path, 'follow_links': False})
            if source.collector != 'demo' or not source.approved or source.allowed_paths != path:
                raise CommandError('A demo source was modified; use an isolated test DATA_DIR.')
            campaign.sources.add(source)
            job = start_setup(source, campaign)
            self.stdout.write(f'{name}: setup #{job.pk} queued for the existing worker.')
