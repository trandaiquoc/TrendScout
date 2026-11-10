"""Offline-only AI contracts. No provider SDK or runtime integration."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

class Task(str, Enum):
    TRANSLATION = 'translation'
    CLASSIFICATION = 'classification'
    NER = 'ner_enrichment'
    EXPLANATION = 'trend_explanation'
    COMMERCIAL = 'commercial_relevance'
    IDEAS = 'product_ideas'
    SUMMARY = 'summary'

@dataclass(frozen=True)
class Metadata:
    provider_id: str
    provider_version: str
    model_id: str
    model_version: str
    tasks: frozenset[Task]
    output_modes: frozenset[str]
    context_tokens: int
    output_tokens: int
    timeout: bool
    cancellation: bool
    usage_reporting: bool
    error_classes: frozenset[str]

@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

@dataclass(frozen=True)
class Reply:
    text: str = ''
    usage: Usage | None = None
    refusal: bool = False
    truncated: bool = False

class Provider(Protocol):
    metadata: Metadata
    def invoke(self, instructions: str, source_data: str, *, timeout: float, max_output_tokens: int) -> Reply: ...

class SecretResolver(Protocol):
    """Future server-only boundary; no implementation or credential lookup here."""
    def resolve(self, provider_id: str) -> object: ...
    def revoke(self, provider_id: str) -> None: ...

class ProviderFailure(Exception):
    """Only fixed error codes escape; never the provider's message."""
    def __init__(self, code: str, *, retry_after: float = 0, acknowledged: bool = False):
        super().__init__(code if code in ERROR_CODES else 'PERMANENT_PROVIDER_ERROR')
        self.code = str(self)
        self.retry_after = retry_after
        self.acknowledged = acknowledged

ERROR_CODES = frozenset({'INVALID_REQUEST','POLICY_DENIED','BUDGET_EXCEEDED','RATE_LIMITED',
    'TIMEOUT','TRANSIENT_PROVIDER_ERROR','INVALID_OUTPUT','PROVIDER_UNAVAILABLE',
    'PERMANENT_PROVIDER_ERROR','CANCELLED'})

@dataclass(frozen=True)
class Request:
    task: Task
    provider_id: str
    model_id: str
    prompt_version: str
    schema_version: str
    policy_version: str
    request_id: str
    audit_id: str
    source_data: str = field(repr=False)
    input_fingerprint: str
    data_categories: frozenset[str]
    permission: bool
    max_estimated_input_tokens: int
    max_output_tokens: int
    deadline: float
    language_pair: str = ''
    request_version: str = 'synthetic-request-v1'
    source_permission_version: str = 'synthetic-permission-v1'

@dataclass(frozen=True)
class Result:
    outcome: str
    value: dict | None = field(default=None, repr=False)
    cost_status: str = 'COST_UNKNOWN'
    accounting: str = 'NOT_RESERVED'

    @property
    def availability(self):
        if self.outcome == 'SUCCESS': return 'AVAILABLE'
        if self.outcome in ('POLICY_DENIED', 'PROVIDER_UNAVAILABLE'): return 'SKIPPED'
        return 'AI_UNAVAILABLE'
