"""Strict, bounded synthetic response contract, never auto-repair provider data."""
import json
from .contracts import Task

class InvalidOutput(ValueError):
    def __init__(self):
        super().__init__('INVALID_OUTPUT')

def validate(reply, schema_version, request=None):
    if schema_version not in ('synthetic-v1', 'translation-v1') or reply.refusal or reply.truncated or not isinstance(reply.text, str):
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
        if schema_version == 'translation-v1':
            return _translation(obj, request)
        if request is not None and request.task == Task.TRANSLATION:
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
