"""Pure contracts. Provider text never controls permissions, URLs or spending."""
import hashlib
import json
import math
from django.conf import settings
from django.utils.module_loading import import_string


class ContractError(ValueError):
    pass


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


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'model', 'state', 'questions'}:
        raise ContractError('Expected model, state and questions.')
    if request['model'] != settings.JEV_MODEL or request['model'] in ('jev-latest', 'jev-preview'):
        raise ContractError('An exact configured model version is required.')
    if not isinstance(request['state'], dict) or not isinstance(request['questions'], dict) or not request['questions']:
        raise ContractError('Structured state and nonempty questions required.')
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
    for key, answer in answers.items():
        if not isinstance(answer, dict) or answer.get('type') != 'choice':
            raise ContractError('Invalid answer type.')
        probabilities = answer.get('probabilities')
        criteria = request['questions'][key]['criteria']
        if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
            raise ContractError('Response labels do not match supplied choices.')
        values = list(probabilities.values()) + [answer.get('confidence')]
        if any(type(v) not in (float, int) or not 0 <= v <= 1 or not math.isfinite(v) for v in values):
            raise ContractError('Invalid probability/confidence.')
        if abs(sum(probabilities.values()) - 1) > 0.001:
            raise ContractError('Probabilities must sum to one.')
        label = answer.get('choice')
        if not isinstance(label, str) or label not in criteria or probabilities[label] < max(probabilities.values()) - 0.001:
            raise ContractError('Invalid winning choice.')
    return answers


def usage_of(response):
    usage = response.get('usage', {}) if isinstance(response, dict) else {}
    if isinstance(usage, dict) and all(type(usage.get(k)) is int and 0 <= usage[k] <= 10**9 for k in ('input_tokens', 'output_tokens')):
        return usage
    return None
