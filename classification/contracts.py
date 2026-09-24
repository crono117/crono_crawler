"""Pure contracts. Provider text never controls permissions, URLs or spending."""
import hashlib
import json
import math
from decimal import Decimal
from django.conf import settings
from django.utils.module_loading import import_string


class ContractError(ValueError):
    pass


PROBABILITY_SUM_TOLERANCE = 0.001
PROBABILITY_ROUNDING_NORMALIZATION_LIMIT = Decimal('0.01')
PROBABILITY_EXACT_EPSILON = math.ulp(1.0)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def input_count(request):
    # Count the entire envelope, including all questions/criteria. This fallback is
    # an estimate, NOT the provider tokenizer or a billing guarantee.
    if settings.JEV_TOKEN_COUNTER:
        result = import_string(settings.JEV_TOKEN_COUNTER)(request)
        if type(result) is not int or result < 1:
            raise ContractError('Configured token counter must return a positive integer.')
        return result, settings.JEV_TOKEN_COUNTER, True
    count = math.ceil(len(canonical(request).encode('utf-8')) * 1.25) + 512
    return count, 'utf8-envelope-plus-25pct-and-512-v1', False


def packet_state(state, question_keys):
    """Return only evidence referenced by this packet's question IDs."""
    result = state
    if 'contacts' in state:
        contact_ids = {int(key.removeprefix('contact_')) for key in question_keys
                       if key.startswith('contact_') and key.removeprefix('contact_').isdigit()}
        result = result | {'contacts': [item for item in state['contacts'] if item['id'] in contact_ids]}
    if state.get('blocks'):
        block_ids = {key.removeprefix('candidate_block_') for key in question_keys
                     if key.startswith('candidate_block_')}
        if block_ids:
            result = result | {'blocks': [block for block in state['blocks'] if block['id'] in block_ids]}
            if 'page_purpose' not in question_keys:
                result = {key: value for key, value in result.items() if key != 'spans'}
    return result


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'model', 'state', 'questions'}:
        raise ContractError('Expected model, state and questions.')
    if request['model'] != settings.JEV_MODEL or request['model'] in ('jev-latest', 'jev-preview'):
        raise ContractError('An exact configured model version is required.')
    if not isinstance(request['state'], dict) or not isinstance(request['questions'], dict) or not request['questions']:
        raise ContractError('Structured state and nonempty questions required.')
    block_questions = {key.removeprefix('candidate_block_') for key in request['questions']
                       if key.startswith('candidate_block_')}
    if block_questions:
        blocks = request['state'].get('blocks')
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 6:
            raise ContractError('Candidate block questions require one to six supplied blocks.')
        block_ids = []
        for block in blocks:
            if (not isinstance(block, dict) or set(block) != {'id', 'span_id', 'text'} or
                    not isinstance(block['id'], str) or not block['id'] or
                    type(block['span_id']) is not int or block['span_id'] < 1 or
                    not isinstance(block['text'], str) or not block['text'] or len(block['text']) > 500):
                raise ContractError('Invalid supplied candidate block.')
            block_ids.append(block['id'])
        if len(block_ids) != len(set(block_ids)) or set(block_ids) != block_questions:
            raise ContractError('Candidate block question IDs must exactly match supplied blocks.')
        if sum(len(block['text']) for block in blocks) > 2400:
            raise ContractError('Candidate block text exceeds the aggregate bound.')
    for question in request['questions'].values():
        if not isinstance(question, dict) or set(question) != {'type', 'instructions', 'criteria'} or question['type'] != 'choice':
            raise ContractError('Only the versioned Choice contract is enabled.')
        if not isinstance(question['instructions'], str) or not question['instructions'].strip():
            raise ContractError('Question instructions required.')
        criteria = question['criteria']
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255 or 'unknown' not in criteria:
            raise ContractError('Bounded criteria with an unknown outcome required.')
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in criteria.items()):
            raise ContractError('Criteria must be string labels and descriptions.')
    return input_count(request)


def validate_response(request, response):
    if not isinstance(response, dict) or response.get('model') != request['model']:
        raise ContractError('Unexpected or missing model version.')
    answers = response.get('answers')
    if not isinstance(answers, dict) or set(answers) != set(request['questions']):
        raise ContractError('Response question IDs do not match.')
    validated = {}
    normalizations = []
    for ordinal, (key, answer) in enumerate(answers.items(), start=1):
        if not isinstance(answer, dict) or answer.get('type') != 'choice':
            raise ContractError('Invalid answer type.')
        probabilities = answer.get('probabilities')
        criteria = request['questions'][key]['criteria']
        if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
            raise ContractError('Response labels do not match supplied choices.')
        values = list(probabilities.values()) + [answer.get('confidence')]
        if any(type(v) not in (float, int) or not 0 <= v <= 1 or not math.isfinite(v) for v in values):
            raise ContractError('Invalid probability/confidence.')
        total = math.fsum(probabilities.values())
        decimal_delta = abs(sum((Decimal(str(value)) for value in probabilities.values()), Decimal()) - Decimal(1))
        delta = float(decimal_delta)
        if delta > PROBABILITY_EXACT_EPSILON:
            if total > 0 and decimal_delta <= PROBABILITY_ROUNDING_NORMALIZATION_LIMIT:
                normalized = {label: value / total for label, value in probabilities.items()}
                peak = max(normalized.items(), key=lambda item: item[1])[0]
                normalized[peak] += 1 - math.fsum(normalized.values())
                probabilities = normalized
                normalizations.append({
                    'question_id': key, 'original_sum': total, 'delta': delta,
                    'choices': len(probabilities),
                })
            else:
                raise ContractError(
                    f'Probability sum invalid at answer {ordinal}: choices={len(probabilities)} '
                    f'sum={total:.6f} delta={delta:.6f}.')
        label = answer.get('choice')
        if (not isinstance(label, str) or label not in criteria or
                probabilities[label] < max(probabilities.values()) - PROBABILITY_SUM_TOLERANCE):
            raise ContractError('Invalid winning choice.')
        validated[key] = {
            'type': 'choice', 'choice': label, 'probabilities': probabilities,
            'confidence': answer['confidence'],
        }
    return validated, normalizations


def usage_of(response):
    usage = response.get('usage', {}) if isinstance(response, dict) else {}
    if isinstance(usage, dict) and all(type(usage.get(k)) is int and 0 <= usage[k] <= 10**9 for k in ('input_tokens', 'output_tokens')):
        return usage
    return None
