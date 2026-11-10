"""Synthetic-only, in-memory product research; independent of application runtime."""
from dataclasses import asdict, dataclass, replace, field
from datetime import date, datetime
from hashlib import sha256
import json
import math
import re
from app.taxonomy.registry import CATEGORIES, TAXONOMY_VERSION

POLICY_VERSION = 'research-synthetic-v1'
CONTRACT_VERSION = 'product-research-v1'
FORMULA_VERSION = 'synthetic-intent-index-v1'
AI_ENABLED = False


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def _day(value):
    if not isinstance(value, str): raise ValueError('invalid date')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value: raise ValueError('invalid date')
    return parsed

def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None: raise ValueError('timezone required')
    return parsed

@dataclass(frozen=True)
class Case:
    case_id: str
    tenant: str
    keyword_id: str
    title_fingerprint: str
    dataset_id: str
    source: str
    project: str
    country: str
    access: str
    window_start: str
    window_end: str
    algorithm_id: str
    algorithm_version: str
    attention_score: float | None
    attention_metric: str
    attention_unit: str
    taxonomy_version: str
    classification_version: str
    categories: tuple[str, ...]
    policy_version: str
    created_at: str
    updated_at: str
    evidence_ids: tuple[str, ...] = ()
    review_status: str = 'NOT_REVIEWED'
    contract_version: str = CONTRACT_VERSION
    classification_review_version: str = 'unreviewed'

@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    evidence_type: str
    claim_id: str
    entity_id: str
    source_id: str
    source_type: str
    reference: str
    observed_at: str
    collected_at: str
    window_start: str
    window_end: str
    market: str
    metric: str | None
    value: float | None
    unit: str | None
    license_version: str
    commercial_permission: bool
    sensitivity: str
    owner: str
    shared_allowed: bool
    source_fingerprint: str
    content_digest: str
    verification: str
    expires_at: str
    mode: str = 'SYNTHETIC_ONLY'
    idea_id: str | None = None

    def integrity(self):
        data = asdict(self); data.pop('content_digest')
        return digest(data)

@dataclass(frozen=True)
class Claim:
    claim_id: str
    claim_type: str
    entity_id: str
    evidence_ids: tuple[str, ...]
    source_id: str
    market: str
    window_start: str
    window_end: str
    metric: str | None = None
    value: float | None = None
    unit: str | None = None
    idea_id: str | None = None

@dataclass(frozen=True)
class Verification:
    status: str
    reason: str
    evidence_ids: tuple[str, ...] = ()
    review_status: str = 'REVIEW_REQUIRED'

@dataclass(frozen=True)
class Relevance:
    status: str
    reason: str
    evidence_ids: tuple[str, ...] = ()
    demand: str = 'UNKNOWN'

@dataclass(frozen=True)
class Opportunity:
    score: float | None
    status: str
    formula_version: str | None = None
    mode: str | None = None
    evidence_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class Risk:
    flags: tuple[str, ...]
    status: str = 'REVIEW_REQUIRED'
    legal_conclusion: str = 'NOT_EVALUATED'

@dataclass(frozen=True)
class Idea:
    idea_id: str
    case_id: str
    title: str = field(repr=False)
    category: str = ''
    subcategory: str | None = None
    concept_type: str = 'research_hypothesis'
    customer_hypothesis: str = field(default='', repr=False)
    use_case_hypothesis: str = field(default='', repr=False)
    trend_evidence: tuple[str, ...] = ()
    commercial_evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ('demand_unknown',)
    uncertainties: tuple[str, ...] = ('no_market_validation',)
    risk: Risk = Risk(('IP_NOT_CLEARED',))
    evidence_gaps: tuple[str, ...] = ('commercial_evidence_unverified',)
    status: str = 'UNVERIFIED'
    provider: str = 'none'
    permission_version: str = ''
    created_at: str = ''
    fingerprint: str = ''

@dataclass(frozen=True)
class Summary:
    case_id: str
    relevance: Relevance
    opportunity: Opportunity
    attention_score: float | None
    idea_count: int
    evidence_coverage: float | None
    unsupported_claims: int
    needs_review_rate: float | None
    risk_flag_rate: float | None
    rejected_evidence_count: int
    unverified_ideas: int
    unsupported_claim_rate: float | None
    commercial_score_available: bool
    missing_commercial_data: bool
    mode: str = 'SYNTHETIC_ONLY'

# Synthetic evidence is accepted only from explicitly declared fake source types.
_TYPES = {
    'ATTENTION': ('wikimedia_pageviews_top_per_country', 'WIKIMEDIA', 'views_ceil', 'approx_pageviews_upper_bound'),
    'PRODUCT_EXISTENCE': ('synthetic-commercial-fixture', 'SYNTHETIC_COMMERCIAL', None, None),
    'SEARCH_INTENT': ('synthetic-commercial-fixture', 'SYNTHETIC_COMMERCIAL', 'purchase_intent_index', 'index_0_100'),
    'LISTING_PRICE': ('synthetic-commercial-fixture', 'SYNTHETIC_COMMERCIAL', 'price', 'synthetic_currency'),
    'SALES_CONVERSION': ('synthetic-commercial-fixture', 'SYNTHETIC_COMMERCIAL', 'sales', 'synthetic_units'),
    'CUSTOMER_FEEDBACK': ('synthetic-commercial-fixture', 'SYNTHETIC_COMMERCIAL', None, None),
    'MANUAL_HYPOTHESIS': ('synthetic-manual-fixture', 'SYNTHETIC_HYPOTHESIS', None, None),
    'AI_HYPOTHESIS': ('synthetic-ai-fixture', 'SYNTHETIC_HYPOTHESIS', None, None),
}
_CLAIM_TYPES = {'ATTENTION':'ATTENTION', 'PRODUCT_EXISTENCE':'PRODUCT_EXISTENCE',
                'SEARCH_INTENT':'SEARCH_INTENT', 'LISTING_PRICE':'LISTING_PRICE',
                'SALES':'SALES_CONVERSION', 'CUSTOMER_FEEDBACK':'CUSTOMER_FEEDBACK'}

class FakeResearchRepository:
    """Tenant-scoped research namespace. No reference to dataset cleanup or Firestore."""
    namespace = 'tenant_private_research_fake'
    def __init__(self, authority, *, shared_writer=None):
        self.authority = authority
        self._shared_writer = shared_writer
        self._cases, self._evidence, self._ideas = {}, {}, {}
        self.audit = []
    def tenant(self, token):
        tenant = self.authority.resolve(token)
        if not tenant: raise ValueError('access denied')
        return tenant
    def put_case(self, token, case):
        tenant = self.tenant(token)
        if case.tenant != tenant: raise ValueError('access denied')
        key = (tenant, case.case_id)
        if key in self._cases and self._cases[key] != case: raise ValueError('case exists')
        self._cases[key] = case
    def case(self, token, case_id):
        return self._cases.get((self.tenant(token), case_id))
    def evidence(self, token, evidence_id):
        tenant = self.tenant(token)
        own = self._evidence.get((tenant, evidence_id))
        if own is not None: return own
        shared = self._evidence.get(('SHARED', evidence_id))
        return shared if shared is not None and shared.shared_allowed and shared.commercial_permission else None
    def register_trusted_evidence(self, token, evidence, *, shared_writer=None):
        """Trusted synthetic fixture boundary; no external data ingestion implemented."""
        tenant = self.tenant(token)
        if evidence.owner == 'SHARED' and (self._shared_writer is None or shared_writer is not self._shared_writer):
            raise ValueError('shared write denied')
        if evidence.owner not in (tenant, 'SHARED') or (evidence.owner == 'SHARED' and not evidence.shared_allowed):
            raise ValueError('access denied')
        if evidence.mode != 'SYNTHETIC_ONLY' or evidence.evidence_type not in _TYPES:
            raise ValueError('unapproved evidence')
        expected = _TYPES[evidence.evidence_type]
        if (evidence.source_id, evidence.source_type, evidence.metric, evidence.unit) != expected:
            raise ValueError('source contract mismatch')
        if not evidence.reference.startswith('fixture:') or not evidence.license_version or not evidence.source_fingerprint:
            raise ValueError('invalid source provenance')
        if evidence.sensitivity not in ('public_synthetic', 'tenant_private') or evidence.verification != 'VERIFIED_SYNTHETIC':
            raise ValueError('invalid synthetic verification')
        if evidence.owner == 'SHARED' and evidence.sensitivity != 'public_synthetic': raise ValueError('private sharing denied')
        if not evidence.commercial_permission: raise ValueError('permission missing')
        if evidence.value is not None and (type(evidence.value) not in (int,float) or not math.isfinite(evidence.value) or evidence.value < 0):
            raise ValueError('invalid metric')
        if evidence.metric is None and evidence.value is not None: raise ValueError('unexpected metric')
        if evidence.evidence_type == 'SEARCH_INTENT' and evidence.value is not None and evidence.value > 100: raise ValueError('invalid index')
        observed=_timestamp(evidence.observed_at); collected=_timestamp(evidence.collected_at); expiry=_timestamp(evidence.expires_at)
        if (_day(evidence.window_start) > _day(evidence.window_end) or not _day(evidence.window_start) <= observed.date() <= _day(evidence.window_end)
                or collected < observed or expiry <= collected): raise ValueError('invalid window')
        if evidence.content_digest != evidence.integrity(): raise ValueError('tampered evidence')
        key = (evidence.owner, evidence.evidence_id)
        if key in self._evidence and self._evidence[key] != evidence: raise ValueError('evidence exists')
        self._evidence[key] = evidence
    def put_idea(self, token, idea):
        tenant = self.tenant(token)
        case=self._cases.get((tenant,idea.case_id))
        if case is None: raise ValueError('access denied')
        values=(idea.title,idea.customer_hypothesis,idea.use_case_hypothesis)
        if (idea.status not in ('UNVERIFIED','REVIEW_REQUIRED') or idea.provider != 'none'
                or idea.concept_type != 'research_hypothesis' or not idea.permission_version
                or not all(isinstance(x,str) and 0 < len(x.strip()) <= 200 for x in values)
                or any(_FORBIDDEN.search(x) or re.search(r'\d|\b[\w-]+\.(?:com|net|org|invalid)\b|[\w.+-]+@[\w.-]+',x,re.I) for x in values)
                or idea.category not in CATEGORIES or CATEGORIES[idea.category].parent_id is not None
                or idea.category == 'unclassified' or (idea.subcategory is not None and
                    (idea.subcategory not in CATEGORIES or CATEGORIES[idea.subcategory].parent_id != idea.category))
                or not idea.trend_evidence or not set(idea.trend_evidence) <= set(case.evidence_ids)
                or idea.commercial_evidence or idea.assumptions != ('demand_unknown',)
                or idea.uncertainties != ('no_market_validation',) or idea.evidence_gaps != ('commercial_evidence_unverified',)
                or not isinstance(idea.risk,Risk) or 'IP_NOT_CLEARED' not in idea.risk.flags
                or idea.risk.legal_conclusion != 'NOT_EVALUATED'):
            raise ValueError('invalid idea')
        _timestamp(idea.created_at)
        expected_flags=set(assess_risk(' '.join(values), permission_known=True).flags)
        if not expected_flags <= set(idea.risk.flags) or idea.risk.status != 'REVIEW_REQUIRED': raise ValueError('invalid risk')
        if any(flag in idea.risk.flags for flag in ('COUNTERFEIT_IMITATION','UNSUPPORTED_HEALTH_CLAIM','RESTRICTED_CATEGORY')) and idea.status != 'REVIEW_REQUIRED':
            raise ValueError('risk review required')
        for eid in idea.trend_evidence:
            ev=self.evidence(token,eid)
            if (ev is None or ev.evidence_type != 'ATTENTION' or ev.entity_id != case.keyword_id
                    or ev.integrity() != ev.content_digest or _timestamp(ev.expires_at) <= _timestamp(idea.created_at)):
                raise ValueError('invalid trend evidence')
        body=[case.case_id,idea.title,idea.category,idea.subcategory,idea.customer_hypothesis,
              idea.use_case_hypothesis,idea.trend_evidence,idea.permission_version,idea.created_at,asdict(idea.risk)]
        if idea.fingerprint != digest(body) or idea.idea_id != idea.fingerprint: raise ValueError('invalid idea')
        key=(tenant,idea.case_id,idea.idea_id)
        if key in self._ideas and self._ideas[key] != idea: raise ValueError('idea exists')
        self._ideas[key]=idea
    def idea(self, token, case_id, idea_id):
        return self._ideas.get((self.tenant(token),case_id,idea_id))

class ResearchService:
    def __init__(self, repository): self.repo = repository
    def ai_ideas(self, token, case_id):
        # This step has no AI integration; never receives a provider or private notes.
        if self.repo.case(token,case_id) is None: raise ValueError('access denied')
        return Verification('SKIPPED', 'AI_DISABLED')
    def create_case(self, token, row, trend, *, dataset_id, window_start, window_end, created_at):
        tenant = self.repo.tenant(token)
        if _day(window_start) > _day(window_end) or not dataset_id: raise ValueError('invalid context')
        _timestamp(created_at)
        if (trend.keyword_id,trend.source,trend.project,trend.country_scope,trend.access,trend.observation_date) != (row.keyword_id,row.source,row.project,row.country_scope,row.access,row.observation_date):
            raise ValueError('trend identity mismatch')
        if row.observation_date != _day(window_end): raise ValueError('window mismatch')
        if (trend.article_original != row.article_original or trend.categories != row.categories
                or trend.algorithm_version != 'wikimedia-trend-v0.1'
                or (trend.score is not None and (type(trend.score) not in (int,float) or not math.isfinite(trend.score) or not 0 <= trend.score <= 100))):
            raise ValueError('invalid trend provenance')
        identity = [tenant,row.keyword_id,row.source,row.project,row.country_scope,row.access,window_start,window_end,dataset_id]
        case = Case(digest(identity),tenant,row.keyword_id,digest(row.display_keyword),dataset_id,row.source,row.project,row.country_scope,
                    row.access,window_start,window_end,'wikimedia-trend',trend.algorithm_version,trend.score,
                    'trend_score','points_0_100',TAXONOMY_VERSION,row.classification_version,row.categories,
                    POLICY_VERSION,created_at,created_at)
        self.repo.put_case(token,case); return case
    def verify_claim(self, token, case_id, claim, *, as_of):
        case = self.repo.case(token,case_id)
        if case is None: return Verification('REJECTED','CASE_NOT_FOUND')
        try: now=_timestamp(as_of)
        except (ValueError,TypeError): return Verification('REJECTED','INVALID_TIME')
        if not isinstance(claim,Claim) or not claim.evidence_ids: return Verification('UNVERIFIED','NO_EVIDENCE')
        if claim.entity_id != case.keyword_id or claim.market != case.country or (claim.window_start,claim.window_end)!=(case.window_start,case.window_end):
            return Verification('REJECTED','CLAIM_SCOPE_MISMATCH')
        if claim.idea_id is not None and self.repo.idea(token,case_id,claim.idea_id) is None:
            return Verification('REJECTED','IDEA_NOT_FOUND')
        if claim.value is not None and (type(claim.value) not in (int,float) or not math.isfinite(claim.value)):
            return Verification('REJECTED','INVALID_METRIC')
        expected=_CLAIM_TYPES.get(claim.claim_type)
        if expected is None: return Verification('REJECTED','UNSUPPORTED_CLAIM')
        if len(set(claim.evidence_ids)) != len(claim.evidence_ids): return Verification('REJECTED','DUPLICATE_EVIDENCE')
        for eid in claim.evidence_ids:
            if eid not in case.evidence_ids: return Verification('REJECTED','EVIDENCE_NOT_ATTACHED')
            ev=self.repo.evidence(token,eid)
            if ev is None: return Verification('REJECTED','EVIDENCE_NOT_FOUND')
            try:
                if ev.integrity()!=ev.content_digest: return Verification('REJECTED','TAMPERED_EVIDENCE')
                if _timestamp(ev.expires_at)<=now or _timestamp(ev.observed_at)>now or _timestamp(ev.collected_at)>now: return Verification('REJECTED','STALE_OR_FUTURE')
                if not ev.commercial_permission or not ev.license_version or ev.verification!='VERIFIED_SYNTHETIC': return Verification('REJECTED','PERMISSION_OR_VERIFICATION')
                if ev.mode!='SYNTHETIC_ONLY' or (ev.source_id,ev.source_type,ev.metric,ev.unit)!=_TYPES.get(ev.evidence_type): return Verification('REJECTED','SOURCE_CONTRACT')
                if (ev.evidence_type,ev.claim_id,ev.entity_id,ev.source_id,ev.market,ev.window_start,ev.window_end,ev.metric,ev.value,ev.unit,ev.idea_id) != (expected,claim.claim_id,claim.entity_id,claim.source_id,claim.market,claim.window_start,claim.window_end,claim.metric,claim.value,claim.unit,claim.idea_id):
                    return Verification('REJECTED','CLAIM_EVIDENCE_MISMATCH')
            except (ValueError,TypeError): return Verification('REJECTED','INVALID_EVIDENCE')
        return Verification('VERIFIED_SYNTHETIC','CLAIM_MATCHED',claim.evidence_ids)
    def assess(self, token, case_id, claim=None, *, as_of):
        if self.repo.case(token,case_id) is None: raise ValueError('access denied')
        if claim is None: return Relevance('UNKNOWN','NO_COMMERCIAL_EVIDENCE')
        result=self.verify_claim(token,case_id,claim,as_of=as_of)
        if result.status!='VERIFIED_SYNTHETIC': return Relevance('UNKNOWN',result.reason)
        if claim.claim_type=='ATTENTION': return Relevance('UNKNOWN','ATTENTION_ONLY',claim.evidence_ids)
        if claim.claim_type=='PRODUCT_EXISTENCE': return Relevance('POSSIBLY_RELEVANT','PRODUCT_EXISTS_SYNTHETIC',claim.evidence_ids)
        return Relevance('REVIEW_REQUIRED','SYNTHETIC_COMMERCIAL_SIGNAL',claim.evidence_ids)
    def opportunity(self, token, case_id, claim=None, *, as_of):
        if self.repo.case(token,case_id) is None: raise ValueError('access denied')
        if claim is None: return opportunity(None)
        verified=self.verify_claim(token,case_id,claim,as_of=as_of)
        if verified.status!='VERIFIED_SYNTHETIC' or claim.claim_type!='SEARCH_INTENT' or claim.metric!='purchase_intent_index' or claim.unit!='index_0_100' or claim.value is None:
            return opportunity(None)
        return Opportunity(float(claim.value),'SYNTHETIC_ONLY',FORMULA_VERSION,'SYNTHETIC_ONLY',claim.evidence_ids)

    def attach_evidence(self, token, case_id, evidence_id, *, as_of):
        case=self.repo.case(token,case_id); ev=self.repo.evidence(token,evidence_id)
        if case is None or ev is None: raise ValueError('access denied')
        now=_timestamp(as_of)
        if (ev.entity_id!=case.keyword_id or ev.market!=case.country or (ev.window_start,ev.window_end)!=(case.window_start,case.window_end)
                or ev.content_digest!=ev.integrity() or _timestamp(ev.expires_at)<=now or _timestamp(ev.observed_at)>now or _timestamp(ev.collected_at)>now
                or not ev.commercial_permission or not ev.license_version or ev.verification!='VERIFIED_SYNTHETIC'):
            raise ValueError('invalid evidence')
        key=(case.tenant,case.case_id)
        updated=replace(case,evidence_ids=tuple(dict.fromkeys(case.evidence_ids+(evidence_id,))),updated_at=as_of)
        self.repo._cases[key]=updated
        return updated

    def propose_idea(self, token, case_id, *, title, category, subcategory, customer, use_case,
              trend_evidence, created_at, permission_version, entity_type='unknown'):
        case=self.repo.case(token,case_id)
        if case is None: raise ValueError('access denied')
        _timestamp(created_at)
        values=(title,customer,use_case)
        if not all(isinstance(x,str) and 0<len(x.strip())<=200 for x in values): raise ValueError('invalid hypothesis')
        if any(_FORBIDDEN.search(x) or re.search(r'\d|\b[\w-]+\.(?:com|net|org|invalid)\b|[\w.+-]+@[\w.-]+', x, re.I) for x in values): raise ValueError('untrusted claim')
        if not permission_version: raise ValueError('permission missing')
        if category not in CATEGORIES or CATEGORIES[category].parent_id is not None or category=='unclassified': raise ValueError('invalid taxonomy')
        if subcategory is not None and (subcategory not in CATEGORIES or CATEGORIES[subcategory].parent_id!=category): raise ValueError('invalid taxonomy')
        if not trend_evidence or len(set(trend_evidence))!=len(trend_evidence): raise ValueError('evidence required')
        for eid in trend_evidence:
            ev=self.repo.evidence(token,eid)
            if (ev is None or eid not in case.evidence_ids or ev.evidence_type!='ATTENTION' or ev.entity_id!=case.keyword_id
                    or ev.integrity()!=ev.content_digest or _timestamp(ev.expires_at)<=_timestamp(created_at)
                    or _timestamp(ev.observed_at)>_timestamp(created_at) or _timestamp(ev.collected_at)>_timestamp(created_at)
                    or not ev.commercial_permission): raise ValueError('invalid trend evidence')
        risk=assess_risk(' '.join(values),entity_type=entity_type,permission_known=True)
        blocked=any(x in risk.flags for x in ('COUNTERFEIT_IMITATION','UNSUPPORTED_HEALTH_CLAIM','RESTRICTED_CATEGORY'))
        body=[case.case_id,title,category,subcategory,customer,use_case,trend_evidence,permission_version,created_at,asdict(risk)]
        fp=digest(body)
        idea=Idea(fp,case.case_id,title,category,subcategory,'research_hypothesis',customer,use_case,tuple(trend_evidence),(),
                  ('demand_unknown',),('no_market_validation',),risk,('commercial_evidence_unverified',),
                  'REVIEW_REQUIRED' if blocked else 'UNVERIFIED','none',permission_version,created_at,fp)
        self.repo.put_idea(token,idea); return idea

    def summary(self, token, case_id, claims, ideas, *, as_of):
        case=self.repo.case(token,case_id)
        if case is None: raise ValueError('access denied')
        verifies=[self.verify_claim(token,case_id,c,as_of=as_of) for c in claims]
        unsupported=sum(v.status!='VERIFIED_SYNTHETIC' for v in verifies)
        if any(self.repo.idea(token,case_id,i.idea_id) != i for i in ideas): raise ValueError('invalid idea')
        n=len(ideas)
        eligible=[c for c,v in zip(claims,verifies) if v.status=='VERIFIED_SYNTHETIC' and c.claim_type=='SEARCH_INTENT']
        selected=eligible[0] if len(eligible)==1 else None
        op=self.opportunity(token,case_id,selected,as_of=as_of)
        relevance=self.assess(token,case_id,selected,as_of=as_of)
        return Summary(case_id,relevance,op,case.attention_score,n,
                       (len(verifies)-unsupported)/len(verifies) if verifies else None,unsupported,
                       sum(i.status in ('UNVERIFIED','REVIEW_REQUIRED') for i in ideas)/n if n else None,
                       sum(bool(i.risk.flags) for i in ideas)/n if n else None,
                       sum(v.status=='REJECTED' for v in verifies),
                       sum(i.status=='UNVERIFIED' for i in ideas),
                       unsupported/len(verifies) if verifies else None,op.score is not None,op.score is None)

def opportunity(_):
    """No standalone score can be produced from unverified input."""
    return Opportunity(None, 'INSUFFICIENT_COMMERCIAL_EVIDENCE')

# The prototype supports manual hypotheses only; it never constructs or invokes an AI client.
_FORBIDDEN = re.compile(r'(?i)(https?://|www\.|\$|\b(?:sales|sold|revenue|conversion|best.?seller|supplier|price|market share|demand is rising|approve|ignore previous instructions|api[_ -]?key|medical cure)\b|<\s*script\b)')
_RISKS = (
    (re.compile(r'(?i)\b(?:brand|trademark|logo)\b'), 'TRADEMARK_BRAND'),
    (re.compile(r'(?i)\b(?:film|character|franchise|copyright)\b'), 'COPYRIGHT_FRANCHISE'),
    (re.compile(r'(?i)\b(?:singer|celebrity|likeness|person|portrait)\b'), 'PERSON_LIKENESS'),
    (re.compile(r'(?i)\b(?:counterfeit|replica|imitation)\b'), 'COUNTERFEIT_IMITATION'),
    (re.compile(r'(?i)\b(?:medical|cure|treat|health claim)\b'), 'UNSUPPORTED_HEALTH_CLAIM'),
    (re.compile(r'(?i)\b(?:weapon|drug|alcohol)\b'), 'RESTRICTED_CATEGORY'),
    (re.compile(r'(?i)\b(?:private|personal|ugc)\b'), 'PRIVACY'),
)

def assess_risk(text, *, entity_type='unknown', permission_known=False):
    flags=['IP_NOT_CLEARED','ADVERTISING_CLAIMS_UNVERIFIED']
    if not permission_known: flags.append('SOURCE_PERMISSION_UNKNOWN')
    if entity_type in ('person','brand','organization','work'):
        flags.append({'person':'PERSON_LIKENESS','brand':'TRADEMARK_BRAND','organization':'TRADEMARK_BRAND','work':'COPYRIGHT_FRANCHISE'}[entity_type])
    flags += [flag for pattern,flag in _RISKS if pattern.search(text)]
    return Risk(tuple(dict.fromkeys(flags)))
