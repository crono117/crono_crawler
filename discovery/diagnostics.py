"""Counts and fixed error categories only; no exception bodies or rejected contact values."""
from collections import Counter

from leads.services.network import FetchError


def failure_category(exc):
    # Inspect transport messages to classify them, never persist those messages here.
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or (isinstance(exc, FetchError) and ('timed out' in text or 'timeout' in text)):
        return 'timeout'
    if isinstance(exc, FetchError):
        if 'robots' in text:
            return 'robots_invalid' if 'html' in text else 'robots_denied'
        if exc.status in (401, 403):
            return 'access_blocked'
        return 'http_unavailable' if exc.retryable or (exc.status and exc.status >= 500) else 'fetch_error'
    return 'parser_or_extraction_error' if isinstance(exc, (ValueError, TypeError)) else 'worker_error'


def extraction_counts(stats, *, automatic=False):
    """Map either extractor's diagnostics to the same count vocabulary."""
    signals = stats.get('signals', {})
    return {
        'candidate_people': signals.get('name', 0) if automatic else stats.get('candidate_people', 0),
        'candidate_contact_methods': stats.get('candidate_contact_methods', 0),
        'rejected_candidates': dict(stats.get('primary_rejections' if automatic else 'rejected', {})),
        'duplicates': stats.get('primary_rejections', {}).get('duplicate_card', 0) if automatic else stats.get('duplicates', 0),
    }


def summarize(run):
    counts = Counter()
    rejected, failures = Counter(), Counter()
    measured = 0
    for job in run.jobs.all().iterator():
        data = job.diagnostics
        measured += bool(data.get('extraction_measured'))
        for key in ('pages_fetched', 'candidate_people', 'candidate_contact_methods', 'duplicates',
                    'new_non_synthetic_leads', 'worker_interruptions'):
            counts[key] += data.get(key, 0)
        rejected.update(data.get('rejected_candidates', {}))
        if job.status == 'failed':
            failures[job.failure_code or 'legacy_unknown'] += 1
    run.refresh_from_db()
    return {**{key: counts[key] for key in ('pages_fetched', 'candidate_people', 'candidate_contact_methods',
                'duplicates', 'new_non_synthetic_leads', 'worker_interruptions')},
            'pages_saved': run.pages_done, 'accepted_observations': run.contacts_seen,
            'new_contacts_all_collectors': run.new_contacts,
            'rejected_candidates': dict(rejected), 'failures': dict(failures),
            'jobs_with_extraction_diagnostics': measured,
            'coverage': 'partial_or_none' if measured < run.jobs.filter(kind='page', status='done').count() else 'current'}


def terminal(run):
    stats = summarize(run)
    failures = sum(stats['failures'].values())
    code = ('job_failures' if failures else 'extraction_rejected' if not run.contacts_seen and
            stats['rejected_candidates'] else 'no_accepted_contacts' if run.pages_done and not run.contacts_seen else
            'refresh_only' if run.contacts_seen and not run.new_contacts else 'completed')
    categories = ', '.join(f'{key}={value}' for key, value in sorted(stats['failures'].items()))
    message = (f'{failures} job(s) failed ({categories}). ' if failures else '')
    message += f'{run.pages_done} pages saved; {run.contacts_seen} accepted observations; {run.new_contacts} new contacts (all collectors).'
    if not failures and not run.contacts_seen and run.pages_done:
        message += ' Review extraction rejection counts and page suitability.'
    return code, message[:1000], stats
