"""Synthetic cosine candidates only; similarity never merges entities or affects scores."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class Vector:
    values: tuple[float, ...]
    model_version: str
    tenant: str
    source: str
    country: str
    access: str
    date: str

@dataclass(frozen=True)
class SemanticCandidate:
    source_entity: str
    candidate_entity: str
    similarity: float
    model_version: str
    dimension: int
    scope: tuple
    evidence: str = 'SYNTHETIC_VECTOR_ONLY'
    review_status: str = 'REVIEW_REQUIRED'
    similarity_type: str = 'synthetic_cosine'

def candidate(left, right, a, b, *, authority, context):
    tenant = authority.resolve(context)
    if tenant is None: return None, 'UNTRUSTED_CONTEXT'
    if a is None or b is None: return None, 'NO_SEMANTIC_EVIDENCE'
    if not isinstance(a, Vector) or not isinstance(b, Vector): return None, 'INVALID_VECTOR'
    if left.keyword_id == right.keyword_id: return None, 'SAME_ENTITY'
    if min(len(left.display_keyword.strip()), len(right.display_keyword.strip())) < 8: return None, 'SHORT_TITLE'
    scope_a = (a.tenant, a.source, a.country, a.access, a.date)
    scope_b = (b.tenant, b.source, b.country, b.access, b.date)
    if a.tenant != tenant or scope_a != scope_b or (a.source,a.country,a.access,a.date) != (left.source,left.country_scope,left.access,left.observation_date.isoformat()) or (b.source,b.country,b.access,b.date) != (right.source,right.country_scope,right.access,right.observation_date.isoformat()):
        return None, 'INCOMPATIBLE_SCOPE'
    if not a.model_version or a.model_version != b.model_version: return None, 'MODEL_MISMATCH'
    if type(a.values) is not tuple or type(b.values) is not tuple: return None, 'INVALID_VECTOR'
    if not a.values or len(a.values) != len(b.values) or len(a.values) > 1024: return None, 'DIMENSION_MISMATCH'
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in a.values + b.values): return None, 'INVALID_VECTOR'
    norm_a, norm_b = math.hypot(*a.values), math.hypot(*b.values)
    if not norm_a or not norm_b or not math.isfinite(norm_a) or not math.isfinite(norm_b): return None, 'INVALID_VECTOR'
    score = sum((x/norm_a)*(y/norm_b) for x,y in zip(a.values,b.values))
    return SemanticCandidate(left.keyword_id, right.keyword_id, max(-1.0,min(1.0,score)), a.model_version, len(a.values), scope_a), 'CANDIDATE_ONLY'
