"""Offline cost scenarios and identity design; no counters or cloud calls."""
from dataclasses import asdict, dataclass
import hashlib
import json

MODEL_VERSION = 'storage-estimate-v1'


@dataclass(frozen=True)
class Estimate:
    reads: int = 0
    writes: int = 0
    deletes: int = 0
    json_bytes: int = 0

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in asdict(self).values()):
            raise ValueError('estimates must be nonnegative integers')


def estimate_scenario(*, runs, list_requests, detail_requests=0, metadata_requests=0,
                      writes_per_run=232, content_docs_per_run=226,
                      json_bytes_per_run=9_412_755, keyword_shards=16, keyword_bytes=0):
    values = locals().copy()
    if any(type(v) is not int or v < 0 for v in values.values()):
        raise ValueError('scenario values must be nonnegative integers')
    return {'model_version': MODEL_VERSION,
            'reads': list_requests * (1 + max(1, keyword_shards)) + detail_requests * 2 + metadata_requests,
            'writes': runs * writes_per_run, 'deletes': 0,
            'new_content_documents': runs * content_docs_per_run,
            'json_content_bytes': runs * json_bytes_per_run,
            'estimated_keyword_payload_bytes': list_requests * keyword_bytes}


def check_budget(estimate, budget):
    """Fail safe on missing budget. Offline per-operation estimates only."""
    if not isinstance(estimate, Estimate) or not isinstance(budget, Estimate):
        raise ValueError('complete estimate and budget required')
    exceeded = [key.upper() + '_BUDGET_EXCEEDED' for key, value in asdict(estimate).items() if value > asdict(budget)[key]]
    return {'allowed': not exceeded, 'reason_codes': exceeded}


def dataset_key(*, kind, identity, share_allowed):
    """Proposed shared identity, not a Firestore ID or an authorization decision."""
    required = {'source', 'country', 'access', 'project', 'analysis_date', 'window', 'source_version', 'cleaning_version'}
    if kind == 'analysis':
        required |= {'classification_version', 'algorithm_version', 'algorithm_config'}
    elif kind != 'source':
        raise ValueError('invalid dataset kind')
    if share_allowed is not True:
        raise ValueError('sharing not explicitly permitted')
    if not isinstance(identity, dict) or set(identity) != required or any(identity[k] is None or identity[k] == '' for k in required):
        raise ValueError('incomplete or unexpected dataset identity')
    try:
        payload = json.dumps({'kind': kind, 'identity': identity}, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError('identity must be canonical JSON') from exc
    return 'dataset-v1-' + hashlib.sha256(payload.encode('utf-8')).hexdigest()
