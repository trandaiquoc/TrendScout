"""Offline-only candidate enrichment; never writes core data or invokes live providers."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from app.ai.contracts import Request, Task
from app.ai.engine import fingerprint
from app.ai.provider import FakeProvider
from app.processing.language_detection import detect_language
from app.taxonomy.registry import CATEGORIES, TAXONOMY_VERSION
from app.translation.service import _SENSITIVE

VERSION = 'enrichment-v1'
EVIDENCE_VERSION = 'title-evidence-v1'

def digest(parts):
    return sha256(json.dumps(parts, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()

@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    entity_id: str
    observation_id: str
    source: str
    project: str
    country: str
    access: str
    date: str
    source_url: str | None
    context_fingerprint: str
    version: str = EVIDENCE_VERSION
    source_type: str = 'article_title'

    @classmethod
    def from_row(cls, row):
        fp = fingerprint(row.display_keyword)
        identity = [row.keyword_id, row.observation_id, row.source, row.project, row.country_scope,
                    row.access, row.observation_date.isoformat(), row.source_url, fp, EVIDENCE_VERSION]
        return cls(digest(identity), row.keyword_id, row.observation_id, row.source, row.project,
                   row.country_scope, row.access, row.observation_date.isoformat(), row.source_url, fp)

def verify_evidence(row, evidence, entry):
    """Offsets count Python Unicode code points in the unchanged original title."""
    if not isinstance(evidence, Evidence) or evidence != Evidence.from_row(row): return False
    if entry['evidence_id'] != evidence.evidence_id: return False
    start, end = entry['start'], entry['end']
    return 0 <= start < end <= len(row.display_keyword) and row.display_keyword[start:end] == entry['span']

@dataclass(frozen=True)
class EnrichmentPolicy:
    enabled: bool = False
    tasks: frozenset[Task] = frozenset()
    reviewer_requested: bool = False
    license_approved: bool = False
    pii_reviewed: bool = False
    permission_version: str = ''
    version: str = VERSION
    categories: frozenset[str] = frozenset()
    allowed_providers: frozenset[tuple[str, str]] = frozenset()
    allowed_topics: tuple[str, ...] = ('fashion', 'home_living', 'entertainment', 'sports', 'technology', 'gaming', 'unclassified')
    allowed_subtopics: tuple[str, ...] = ('fashion.clothing', 'home_living.appliances', 'entertainment.film', 'sports.teams', 'technology.software', 'gaming.video_games')

@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    task: str
    entity_id: str
    observation_id: str
    input_fingerprint: str
    evidence_fingerprint: str
    taxonomy_version: str
    source: str
    project: str
    country: str
    access: str
    date: str
    tenant_scope: str
    rule_categories: tuple[str, ...]
    rule_version: str
    status: str
    reason: str
    labels: tuple = ()
    mentions: tuple = ()
    entity_type: str = 'unknown'
    commercial_relevance: str = 'not_assessed'
    model_confidence: float | None = None
    calibration: str = 'CALIBRATION_NOT_EVALUATED'
    review_status: str = 'REVIEW_REQUIRED'
    quality_flags: tuple[str, ...] = ()
    source_language: str = ''
    detection_version: str = ''
    detection_reason: str = ''
    permission_version: str = ''
    provider: str = ''
    provider_version: str = ''
    model: str = ''
    model_version: str = ''
    prompt_version: str = ''
    schema_version: str = ''
    accounting: str = 'NOT_RESERVED'
    audit_reference: str = ''
    cache_status: str = 'DISABLED'

class EnrichmentService:
    def __init__(self, engine): self.engine = engine

    def propose(self, row, *, task, context, job, evidence, policy, ai_policy, audit_id,
                deadline, estimated_input=100, max_output=100, provider_id='fake', model_id='synthetic',
                cancel=None, translation=None):
        text = row.display_keyword
        language = detect_language(text)
        fp = fingerprint(text)
        evfp = digest([evidence.evidence_id, evidence.context_fingerprint, evidence.version]) if isinstance(evidence, Evidence) else ''
        operation = digest([VERSION, task.value if isinstance(task, Task) else '', row.keyword_id,
                            row.observation_id, row.source, row.project, row.country_scope, row.access,
                            row.observation_date.isoformat(), fp, evfp, TAXONOMY_VERSION, row.classification_version,
                            policy.version, policy.permission_version, provider_id, model_id])
        tenant = self.engine.authority.resolve(context)
        base = dict(tenant_scope=digest([tenant]) if tenant else '', proposal_id=operation, task=task.value if isinstance(task, Task) else '', entity_id=row.keyword_id,
                    observation_id=row.observation_id, input_fingerprint=fp, evidence_fingerprint=evfp,
                    taxonomy_version=TAXONOMY_VERSION, source=row.source, project=row.project,
                    country=row.country_scope, access=row.access, date=row.observation_date.isoformat(),
                    rule_categories=row.categories, rule_version=row.classification_version,
                    audit_reference=sha256(audit_id.encode()).hexdigest()[:16], source_language=language.language,
                    detection_version=language.version, detection_reason=language.reason,
                    permission_version=policy.permission_version)
        def result(status, reason, **kw): return Proposal(**base, status=status, reason=reason, **kw)
        if not policy.enabled or task not in policy.tasks or task not in (Task.CLASSIFICATION, Task.NER):
            return result('SKIPPED', 'DISABLED')
        if 'unclassified' not in row.categories and not policy.reviewer_requested:
            return result('SKIPPED', 'NOT_SELECTED')
        if language.status != 'DETECTED': return result('SKIPPED', 'LANGUAGE_UNKNOWN')
        # A translation is never substituted for authoritative source evidence.
        if translation is not None and (translation.entity_id != row.keyword_id or translation.observation_id != row.observation_id
                                        or translation.original_fingerprint != fp or translation.schema_version != 'translation-v1'
                                        or translation.request_version != 'translation-request-v1'):
            return result('SKIPPED', 'TRANSLATION_BINDING_MISMATCH')
        if not policy.license_approved or not policy.pii_reviewed or not policy.permission_version:
            return result('SKIPPED', 'PERMISSION_OR_PII_UNVERIFIED')
        if not policy.categories or not policy.categories <= ai_policy.categories:
            return result('SKIPPED', 'DATA_CATEGORY_DENIED')
        if _SENSITIVE.search(text): return result('SKIPPED', 'SENSITIVE_OR_UNTRUSTED_CONTENT')
        if tenant is None: return result('SKIPPED', 'UNTRUSTED_CONTEXT')
        if cancel is not None and cancel.is_set(): return result('UNAVAILABLE', 'CANCELLED')
        if (provider_id, model_id) not in policy.allowed_providers: return result('SKIPPED', 'PROVIDER_NOT_ALLOWED')
        if not isinstance(evidence, Evidence) or evidence != Evidence.from_row(row):
            return result('REJECTED', 'INVALID_EVIDENCE')
        try:
            provider = self.engine.registry.get(provider_id, model_id, task)
        except Exception:
            return result('UNAVAILABLE', 'PROVIDER_UNAVAILABLE')
        if type(provider) is not FakeProvider: return result('UNAVAILABLE', 'PROVIDER_UNAVAILABLE')
        if any(x not in CATEGORIES or CATEGORIES[x].parent_id is not None for x in policy.allowed_topics):
            return result('SKIPPED', 'INVALID_TAXONOMY_POLICY')
        if any(x not in CATEGORIES or CATEGORIES[x].parent_id not in policy.allowed_topics for x in policy.allowed_subtopics):
            return result('SKIPPED', 'INVALID_TAXONOMY_POLICY')
        schema = 'classification-v1' if task is Task.CLASSIFICATION else 'ner-v1'
        req = Request(task, provider_id, model_id, 'enrichment-prompt-v1', schema, ai_policy.version,
                      operation, audit_id, text, fp, policy.categories, True, estimated_input, max_output,
                      deadline, request_version=('classification-request-v1' if task is Task.CLASSIFICATION else 'ner-request-v1'),
                      source_permission_version=policy.permission_version, entity_id=row.keyword_id,
                      taxonomy_version=TAXONOMY_VERSION, source_language=language.language,
                      evidence_ids=(evidence.evidence_id,), model_version=provider.metadata.model_version,
                      allowed_categories=policy.allowed_topics, allowed_subcategories=policy.allowed_subtopics)
        ai = self.engine.execute(context, job, req, ai_policy, cancel=cancel)
        meta = dict(provider=provider_id, provider_version=provider.metadata.provider_version, model=model_id, model_version=provider.metadata.model_version,
                    prompt_version=req.prompt_version, schema_version=schema, accounting=ai.accounting)
        if ai.outcome != 'SUCCESS':
            return result('REJECTED' if ai.outcome == 'INVALID_OUTPUT' else 'UNAVAILABLE', ai.outcome, **meta)
        entries = ai.value['labels' if task is Task.CLASSIFICATION else 'mentions']
        if any(not verify_evidence(row, evidence, entry) for entry in entries):
            return result('REJECTED', 'INVALID_EVIDENCE', **meta)
        if not entries: return result('REVIEW_REQUIRED', 'NO_EVIDENCE', **meta)
        frozen = tuple(tuple(sorted(entry.items())) for entry in entries)
        if task is Task.CLASSIFICATION:
            flags = ('EVIDENCE_IS_NOT_INFERENCE_PROOF', 'CALIBRATION_NOT_EVALUATED')
            if any(e['reason'] == 'AMBIGUOUS' or e['category'] in ('other', 'unclassified') for e in entries):
                flags += ('AMBIGUOUS',)
            return result('PROPOSED', 'EVIDENCE_VERIFIED', labels=frozen, entity_type=ai.value['entity_type'],
                          model_confidence=ai.value['confidence'], quality_flags=flags, **meta)
        mentions = tuple(tuple(sorted({**entry, 'candidate_id': digest([operation, entry['start'], entry['end'],
                                                                          entry['entity_type']]),
                                        'source_language': language.language}.items())) for entry in entries)
        return result('PROPOSED', 'EVIDENCE_VERIFIED', mentions=mentions,
                      quality_flags=('ENTITY_IDENTITY_UNVERIFIED',), **meta)
