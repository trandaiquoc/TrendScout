"""Explicit offline translation sidecar. Never wired to production RunService."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
import unicodedata

from app.ai.contracts import Request, Task
from app.ai.engine import fingerprint
from app.ai.provider import FakeProvider
from app.processing.language_detection import detect_language

TARGETS = frozenset({'en', 'vi'})
TARGET_POLICY_VERSION = 'targets-v1'
QUALITY_VERSION = 'translation-quality-v1'
NORMALIZATION_VERSION = 'nfc-compare-v1'
# These hints are a conservative blocklist, not a general-purpose PII detector.
_SENSITIVE = re.compile(r'(?i)(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:sk|api[_-]?key|password|authorization|bearer)[-_:= ]+[\w-]{4,}|\b(?:\+?\d[\d ()-]{7,}\d)\b|https?://|ignore\s+(?:all\s+)?previous\s+instructions|<\s*system\b)')
_TOKENS = re.compile(r'\{\{[\w.]+\}\}|\{[\w.]+\}|%(?:\([\w.]+\))?[sdif]|https?://[^\s]+|\b[A-Za-z]+[-_]\d[\w-]*\b')
_NUMBERS = re.compile(r'(?<!\w)\d+(?:[.,:/-]\d+)*(?:\s?%|\s?(?:kg|g|mg|km|cm|mm|ml|l|inch|inches|lb|lbs))?(?!\w)', re.I)
_ACRONYM = re.compile(r'\b[A-Z]{2,}\b')
_PROPER = re.compile(r'\b[A-Z][a-z]{2,}\b')
_INJECTION = re.compile(r'(?i)<\s*/?\s*(?:script|iframe|object)\b|javascript:|ignore\s+(?:all\s+)?previous\s+instructions')

@dataclass(frozen=True)
class TranslationPolicy:
    """Injected by trusted offline caller; not a production authorization boundary."""
    enabled: bool = False
    version: str = 'translation-policy-v1'
    permission_version: str = ''
    license_approved: bool = False
    pii_reviewed: bool = False
    allowed_categories: frozenset[str] = frozenset()
    targets: frozenset[str] = TARGETS
    target_version: str = TARGET_POLICY_VERSION
    protected_entities: tuple[str, ...] = ()
    preserve_original: bool = False

@dataclass(frozen=True)
class Sidecar:
    entity_id: str
    observation_id: str
    original_text: str = field(repr=False)
    original_fingerprint: str = ''
    source: str = ''
    project: str = ''
    country_scope: str = ''
    access: str = ''
    observation_date: str = ''
    source_language: str = 'unknown'
    detection_method: str = ''
    detection_version: str = ''
    detection_status: str = ''
    detection_reason: str = ''
    target_language: str = ''
    purpose: str = ''
    status: str = 'NOT_TRANSLATED'
    reason: str = ''
    translated_text: str | None = field(default=None, repr=False)
    candidate_text: str | None = field(default=None, repr=False)
    quality_flags: tuple[str, ...] = ()
    review_status: str = 'NOT_REVIEWED'
    provider_id: str = ''
    provider_version: str = ''
    model_id: str = ''
    model_version: str = ''
    prompt_version: str = ''
    schema_version: str = 'translation-v1'
    policy_version: str = ''
    permission_version: str = ''
    accounting: str = 'NOT_RESERVED'
    cost_status: str = 'COST_UNKNOWN'
    cache_status: str = 'DISABLED'
    audit_reference: str = ''
    source_url: str | None = None
    detection_confidence: None = None
    request_version: str = 'translation-request-v1'
    target_policy_version: str = TARGET_POLICY_VERSION

    @property
    def display_text(self):
        return self.translated_text if self.status == 'TRANSLATED' else self.original_text

def quality_flags(original, translated, target, protected_entities=()):
    """Conservative flags only; this does not prove semantic equivalence."""
    left = unicodedata.normalize('NFC', original)
    right = unicodedata.normalize('NFC', translated)
    flags = []
    if _TOKENS.findall(left) != _TOKENS.findall(right): flags.append('PLACEHOLDER_MISMATCH')
    if _NUMBERS.findall(left) != _NUMBERS.findall(right): flags.append('NUMBER_OR_UNIT_MISMATCH')
    for name in tuple(protected_entities) + tuple(_ACRONYM.findall(left)) + tuple(_PROPER.findall(left)):
        if name and unicodedata.normalize('NFC', name).casefold() not in right.casefold():
            flags.append('ENTITY_UNVERIFIED'); break
    if _INJECTION.search(right): flags.append('UNTRUSTED_INSTRUCTION_OR_HTML')
    if _SENSITIVE.search(right): flags.append('SENSITIVE_OUTPUT')
    if left.casefold().strip() == right.casefold().strip(): flags.append('UNTRANSLATED_COPY')
    if len(right) < len(left) * 0.25 or len(right) > len(left) * 4: flags.append('LENGTH_ANOMALY')
    target_detection = detect_language(right)
    if target_detection.status != 'DETECTED':
        flags.append('TARGET_LANGUAGE_UNVERIFIED')
    elif target_detection.language != target:
        flags.append('TARGET_LANGUAGE_MISMATCH')
    return tuple(flags)

class TranslationService:
    def __init__(self, engine):
        self.engine = engine

    def translate(self, row, *, context, job, purpose, target, policy, ai_policy,
                  request_id, audit_id, deadline, estimated_input=100, max_output=100,
                  provider_id='fake', model_id='synthetic', cancel=None):
        text = row.display_keyword
        language = detect_language(text)
        base = Sidecar(row.keyword_id, row.observation_id, text, fingerprint(text), row.source,
                       row.project, row.country_scope, row.access, row.observation_date.isoformat(),
                       language.language, language.method, language.version, language.status, language.reason,
                       target, purpose, policy_version=policy.version, permission_version=policy.permission_version,
                       audit_reference=sha256(audit_id.encode()).hexdigest()[:16], source_url=row.source_url)
        def result(status, reason, **fields):
            from dataclasses import replace
            return replace(base, status=status, reason=reason, **fields)
        if not policy.enabled: return result('SKIPPED', 'DISABLED')
        if purpose not in ('NLP', 'UI') or (purpose == 'NLP' and target != 'en') or (purpose == 'UI' and target != 'vi'):
            return result('SKIPPED', 'NO_BUSINESS_PURPOSE')
        if target not in TARGETS or target not in policy.targets or policy.target_version != TARGET_POLICY_VERSION:
            return result('SKIPPED', 'TARGET_NOT_ALLOWED')
        if language.status != 'DETECTED': return result('SKIPPED', 'LANGUAGE_UNKNOWN')
        if language.language == target: return result('SKIPPED', 'ALREADY_TARGET_LANGUAGE')
        if policy.preserve_original: return result('SKIPPED', 'PRESERVE_ORIGINAL')
        if not policy.license_approved or not policy.permission_version or not policy.pii_reviewed:
            return result('SKIPPED', 'PERMISSION_OR_PII_UNVERIFIED')
        if not policy.allowed_categories or not policy.allowed_categories <= ai_policy.categories:
            return result('SKIPPED', 'DATA_CATEGORY_DENIED')
        if _SENSITIVE.search(text): return result('SKIPPED', 'SENSITIVE_OR_UNTRUSTED_CONTENT')
        if self.engine.authority.resolve(context) is None: return result('SKIPPED', 'UNTRUSTED_CONTEXT')
        if cancel is not None and cancel.is_set(): return result('TRANSLATION_UNAVAILABLE', 'CANCELLED')
        try:
            provider = self.engine.registry.get(provider_id, model_id, Task.TRANSLATION)
        except Exception:
            return result('TRANSLATION_UNAVAILABLE', 'PROVIDER_UNAVAILABLE')
        # Explicit offline boundary: even a registered production adapter cannot be invoked here.
        if type(provider) is not FakeProvider:
            return result('TRANSLATION_UNAVAILABLE', 'PROVIDER_UNAVAILABLE')
        meta = provider.metadata
        identity = json.dumps([request_id, row.keyword_id, row.observation_id, row.source, row.project,
                               row.country_scope, row.access, row.observation_date.isoformat(), policy.version,
                               policy.permission_version, QUALITY_VERSION, NORMALIZATION_VERSION, purpose, target],
                              separators=(',', ':'))
        effective_id = sha256(identity.encode()).hexdigest()
        req = Request(Task.TRANSLATION, provider_id, model_id, 'translation-prompt-v1', 'translation-v1',
                      ai_policy.version, effective_id, audit_id, text, fingerprint(text),
                      policy.allowed_categories, True, estimated_input, max_output, deadline,
                      language_pair=language.language + ':' + target,
                      request_version='translation-request-v1', source_permission_version=policy.permission_version,
                      entity_id=row.keyword_id)
        ai = self.engine.execute(context, job, req, ai_policy, cancel=cancel)
        fields = dict(provider_id=provider_id, provider_version=meta.provider_version, model_id=model_id,
                      model_version=meta.model_version, prompt_version=req.prompt_version,
                      accounting=ai.accounting, cost_status=ai.cost_status)
        if ai.outcome != 'SUCCESS': return result('TRANSLATION_UNAVAILABLE', ai.outcome, **fields)
        candidate = ai.value['translated_text']
        flags = quality_flags(text, candidate, target, policy.protected_entities)
        if flags:
            return result('REVIEW_REQUIRED', 'QUALITY_FLAGS',
                          candidate_text=None if 'SENSITIVE_OUTPUT' in flags else candidate,
                          quality_flags=flags, review_status='REVIEW_REQUIRED', **fields)
        return result('TRANSLATED', 'HEURISTIC_CHECKS_PASSED', translated_text=candidate,
                      quality_flags=('SEMANTICS_UNVERIFIED',), review_status='NOT_REVIEWED', **fields)
