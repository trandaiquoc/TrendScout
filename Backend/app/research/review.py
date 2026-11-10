"""Fake research-specific approval; never authorizes production commercialization."""
from dataclasses import asdict, dataclass
from .offline import digest, _timestamp, _TYPES

@dataclass(frozen=True)
class ResearchDecision:
    tenant: str
    case_id: str
    idea_id: str
    evidence_fingerprint: str
    policy_version: str
    decision_version: str
    reviewer: str
    timestamp: str
    reason: str
    status: str
    audit_reference: str

class ResearchReview:
    def __init__(self, repository, reviewer_authority):
        self.repo=repository; self.authority=reviewer_authority
        self.submitted=set(); self.decisions=[]; self.audit=[]
    def submit(self, token, case_id, idea_id):
        tenant=self.repo.tenant(token); case=self.repo.case(token,case_id); idea=self.repo.idea(token,case_id,idea_id)
        if case is None or idea is None: raise ValueError('access denied')
        self.submitted.add((tenant,case_id,idea_id))
    def _current(self, token, case_id, idea_id, as_of):
        case=self.repo.case(token,case_id); idea=self.repo.idea(token,case_id,idea_id)
        if case is None or idea is None: raise ValueError('access denied')
        now=_timestamp(as_of); evidences=[]
        if not case.evidence_ids or not idea.trend_evidence: raise ValueError('evidence missing')
        for eid in case.evidence_ids:
            ev=self.repo.evidence(token,eid)
            if (ev is None or ev.content_digest!=ev.integrity() or not ev.commercial_permission
                    or not ev.license_version or ev.verification!='VERIFIED_SYNTHETIC' or _timestamp(ev.expires_at)<=now
                    or _timestamp(ev.observed_at)>now or _timestamp(ev.collected_at)>now
                    or ev.entity_id!=case.keyword_id or ev.market!=case.country
                    or (ev.window_start,ev.window_end)!=(case.window_start,case.window_end)
                    or ev.mode!='SYNTHETIC_ONLY' or (ev.source_id,ev.source_type,ev.metric,ev.unit)!=_TYPES.get(ev.evidence_type)):
                raise ValueError('evidence stale')
            evidences.append(asdict(ev))
        return case,idea,digest([asdict(case),asdict(idea),evidences])
    def decide(self, token, reviewer_context, case_id, idea_id, status, reason, timestamp):
        tenant=self.repo.tenant(token); principal=self.authority.resolve(reviewer_context)
        if not principal or principal[1]!=tenant or (tenant,case_id,idea_id) not in self.submitted:
            raise ValueError('review denied')
        if status not in ('APPROVED_FOR_RESEARCH','REJECTED','REQUEST_MORE_EVIDENCE') or not isinstance(reason,str) or not reason.strip():
            raise ValueError('invalid decision')
        _timestamp(timestamp); case,idea,fp=self._current(token,case_id,idea_id,timestamp)
        decision=ResearchDecision(tenant,case_id,idea_id,fp,case.policy_version,'research-decision-v1',principal[0],timestamp,reason,status,digest([case_id,idea_id,timestamp])[:16])
        self.decisions.append(decision); self.audit.append({'case':case_id,'reviewer':digest(principal[0]),'status':status})
        return decision
    def valid_for(self, token, decision, *, as_of):
        tenant=self.repo.tenant(token)
        if not isinstance(decision,ResearchDecision) or decision.tenant!=tenant: return False
        latest=next((x for x in reversed(self.decisions) if (x.tenant,x.case_id,x.idea_id)==(tenant,decision.case_id,decision.idea_id)),None)
        if latest is not decision: return False
        try:
            if _timestamp(as_of) < _timestamp(decision.timestamp): return False
            case,idea,fp=self._current(token,decision.case_id,decision.idea_id,as_of)
        except (ValueError,TypeError): return False
        return decision.evidence_fingerprint==fp and decision.policy_version==case.policy_version
