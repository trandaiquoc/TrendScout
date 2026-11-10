"""Strict, bounded synthetic response contract, never auto-repair provider data."""
import json

class InvalidOutput(ValueError):
    def __init__(self):
        super().__init__('INVALID_OUTPUT')

def validate(reply, schema_version):
    if schema_version != 'synthetic-v1' or reply.refusal or reply.truncated or not isinstance(reply.text, str):
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
