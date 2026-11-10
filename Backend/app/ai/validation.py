"""Strict, bounded synthetic response contract, never auto-repair provider data."""
import json
import math
from app.taxonomy.registry import CATEGORIES, TAXONOMY_VERSION
from .contracts import Task

class InvalidOutput(ValueError):
    def __init__(self):
        super().__init__('INVALID_OUTPUT')

def validate(reply, schema_version, request=None):
    if schema_version not in ('synthetic-v1', 'translation-v1', 'classification-v1', 'ner-v1') or reply.refusal or reply.truncated or not isinstance(reply.text, str):
        raise InvalidOutput()
    try:
        raw = reply.text.encode('utf-8', errors='strict')
        if not raw or len(raw) > 8192:
            raise InvalidOutput()
        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise InvalidOutput()
                result[key] = value
            return result
        obj = json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(InvalidOutput()))
        if schema_version in ('classification-v1', 'ner-v1'):
            return _enrichment(obj, request, schema_version)
        if schema_version == 'translation-v1':
            return _translation(obj, request)
        if request is not None and request.task in (Task.TRANSLATION, Task.CLASSIFICATION, Task.NER):
            raise InvalidOutput()
        if type(obj) is not dict or set(obj) != {'kind', 'items'} or obj['kind'] not in ('synthetic',):
            raise InvalidOutput()
        if type(obj['items']) is not list or len(obj['items']) > 20:
            raise InvalidOutput()
        for item in obj['items']:
            if type(item) is not dict or set(item) != {'label', 'value'}:
                raise InvalidOutput()
            if item['label'] not in ('example', 'unknown') or type(item['value']) is not str or len(item['value']) > 512:
                raise InvalidOutput()
            item['value'].encode('utf-8', errors='strict')
        return obj
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidOutput() from None


def _translation(obj, request):
    fields = {'schema', 'source_language', 'target_language', 'input_fingerprint',
              'entity_id', 'request_id', 'translated_text'}
    if request is None or request.task is not Task.TRANSLATION or type(obj) is not dict or set(obj) != fields:
        raise InvalidOutput()
    if any(type(obj[k]) is not str for k in fields):
        raise InvalidOutput()
    if (obj['schema'] != 'translation-v1' or obj['source_language'] not in ('en', 'vi')
            or obj['target_language'] not in ('en', 'vi')
            or obj['source_language'] == obj['target_language']
            or request.language_pair != obj['source_language'] + ':' + obj['target_language']
            or obj['input_fingerprint'] != request.input_fingerprint
            or obj['entity_id'] != request.entity_id or obj['request_id'] != request.request_id
            or not obj['translated_text'].strip() or len(obj['translated_text']) > 2048):
        raise InvalidOutput()
    obj['translated_text'].encode('utf-8', 'strict')
    return obj


ENTITY_TYPES = frozenset({'person', 'organization', 'product', 'place', 'event', 'work', 'unknown'})
REASONS = frozenset({'DIRECT_MENTION', 'CONTEXT', 'AMBIGUOUS'})

def _enrichment(obj, request, schema):
    task = Task.CLASSIFICATION if schema == 'classification-v1' else Task.NER
    common = {'schema', 'entity_id', 'input_fingerprint', 'request_id', 'taxonomy_version', 'source_language', 'model_version'}
    specific = {'labels', 'entity_type', 'confidence'} if task is Task.CLASSIFICATION else {'mentions'}
    if request is None or request.task is not task or type(obj) is not dict or set(obj) != common | specific:
        raise InvalidOutput()
    expected = {'schema': schema, 'entity_id': request.entity_id, 'input_fingerprint': request.input_fingerprint,
                'request_id': request.request_id, 'taxonomy_version': request.taxonomy_version,
                'source_language': request.source_language, 'model_version': request.model_version}
    if any(type(obj[k]) is not str or obj[k] != v for k, v in expected.items()): raise InvalidOutput()
    if obj['taxonomy_version'] != TAXONOMY_VERSION or obj['source_language'] not in ('en', 'vi'): raise InvalidOutput()
    entries = obj['labels'] if task is Task.CLASSIFICATION else obj['mentions']
    if type(entries) is not list or len(entries) > 8: raise InvalidOutput()
    if task is Task.CLASSIFICATION:
        confidence = obj['confidence']
        if confidence is not None and (type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1): raise InvalidOutput()
        if obj['entity_type'] not in ENTITY_TYPES: raise InvalidOutput()
    seen = set()
    for entry in entries:
        fields = ({'category', 'subcategory', 'evidence_id', 'start', 'end', 'span', 'reason'} if task is Task.CLASSIFICATION
                  else {'entity_type', 'evidence_id', 'start', 'end', 'span'})
        if type(entry) is not dict or set(entry) != fields: raise InvalidOutput()
        if (type(entry['evidence_id']) is not str or entry['evidence_id'] not in request.evidence_ids
                or type(entry['start']) is not int or type(entry['end']) is not int
                or entry['start'] < 0 or entry['end'] <= entry['start']
                or type(entry['span']) is not str or not entry['span'] or len(entry['span']) > 512): raise InvalidOutput()
        entry['span'].encode('utf-8', 'strict')
        if task is Task.CLASSIFICATION:
            category, sub = entry['category'], entry['subcategory']
            if type(category) is not str or category not in CATEGORIES or category not in request.allowed_categories or CATEGORIES[category].parent_id is not None or category in seen:
                raise InvalidOutput()
            if sub is not None and (type(sub) is not str or sub not in CATEGORIES or sub not in request.allowed_subcategories or CATEGORIES[sub].parent_id != category): raise InvalidOutput()
            if entry['reason'] not in REASONS: raise InvalidOutput()
            seen.add(category)
        else:
            if entry['entity_type'] not in ENTITY_TYPES: raise InvalidOutput()
            key = (entry['start'], entry['end'], entry['entity_type'])
            if key in seen: raise InvalidOutput()
            seen.add(key)
    if task is Task.CLASSIFICATION and 'unclassified' in seen and len(seen) > 1: raise InvalidOutput()
    return obj
