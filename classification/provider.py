"""One-attempt HTTP transport. No SDK, retries, tools, discovery or credentials in state."""
import asyncio
import json
import re
from dataclasses import dataclass
import httpx
from django.conf import settings

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'


@dataclass
class Result:
    status: int | None
    body: dict | None
    request_id: str = ''
    retry_after: str = ''
    retry_after_ms: str = ''
    error: str = ''


async def _post(request):
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                                        timeout=httpx.Timeout(10, connect=5), trust_env=False, follow_redirects=False) as client:
                async with client.stream('POST', ENDPOINT, json=request, headers={
                    'Authorization': 'Bearer ' + settings.TYPESAFE_API_KEY, 'Content-Type': 'application/json'}) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 256 * 1024:
                            return Result(response.status_code, None, error='response_too_large')
                        chunks.append(chunk)
                    try:
                        body = json.loads(b''.join(chunks))
                    except (ValueError, UnicodeError):
                        body = None
                    rid = response.headers.get('x-request-id', '')
                    # Only simple opaque IDs; never echo arbitrary provider header content.
                    rid = rid[:200] if re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', rid) else ''
                    return Result(response.status_code, body, rid, response.headers.get('retry-after', '')[:100],
                                  response.headers.get('retry-after-ms', '')[:100])
    except (TimeoutError, httpx.TimeoutException):
        return Result(None, None, error='timeout')
    except httpx.HTTPError:
        return Result(None, None, error='transport_error')


def evaluate_once(attempt_id, request, lease_token):
    from .accounting import dispatch
    dispatch(attempt_id, request, lease_token)  # one-use persisted permit, immediately before send
    return asyncio.run(_post(request))


def mock_response(request):
    """Synthetic heuristic for plumbing demos, never presented as Jev accuracy."""
    text = ' '.join(item['text'] for item in request['state']['spans']).lower()
    selected = {}
    for key, question in request['questions'].items():
        label = 'unknown'
        if key == 'company_technology':
            if any(term in text for term in ('develops software', 'develops point-of-sale', 'cloud platform')):
                label = 'technology'
            elif 'restaurant serving' in text:
                label = 'non_technology'
        elif key == 'company_sector':
            if 'point-of-sale' in text:
                label = 'pos_technology'
            elif 'develops software' in text:
                label = 'software'
        elif key == 'company_merchant_services':
            if 'provides payment processing' in text:
                label = 'provider'
            elif 'uses clover' in text:
                label = 'merchant_user'
        elif key == 'page_purpose':
            label = 'company_description'
        elif key == 'person_affiliation' and request['state'].get('company', '').lower() in text:
            label = 'current_supported'
        elif key == 'person_sales_role':
            if 'sales director' in text:
                label = 'sales_leadership'
            elif 'account executive' in text:
                label = 'direct_sales'
            elif 'software engineer' in text:
                label = 'non_sales'
        elif key.startswith('contact_'):
            candidate = next((c for c in request['state'].get('contacts', []) if key == 'contact_' + str(c['id'])), None)
            if candidate:
                label = 'company_shared' if candidate['shared_hint'] else 'person_business'
        options = question['criteria']
        probs = {option: (0.98 if option == label else 0.02 / (len(options) - 1)) for option in options}
        selected[key] = {'type': 'choice', 'choice': label, 'probabilities': probs, 'confidence': 0.96}
    return {'model': request['model'], 'answers': selected, 'usage': {'input_tokens': 0, 'output_tokens': 0}}
