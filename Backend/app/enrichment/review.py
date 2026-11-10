"""Fake reviewer authority and immutable decision records; no public API or RBAC."""
from dataclasses import asdict, dataclass
from datetime import datetime
from .offline import digest, Proposal, Evidence
from app.taxonomy import registry as taxonomy

class ReviewerAuthority:
    def __init__(self): self._tokens = {}
    def issue(self, reviewer, tenant):
        if not reviewer or not tenant: raise ValueError('invalid reviewer')
        token = object(); self._tokens[token] = (reviewer, tenant); return token
    def resolve(self, token):
        try: return self._tokens.get(token)
        except TypeError: return None

@dataclass(frozen=True)
class Decision:
    proposal_id: str
    proposal_fingerprint: str
    taxonomy_version: str
    evidence_fingerprint: str
    reviewer: str
    tenant: str
    timestamp: str
    status: str
    reason: str

class ReviewStore:
    def __init__(self, authority):
        self.authority, self.decisions, self.audit = authority, [], []
        self._proposals = {}
    def register_trusted(self, proposal):
        if not isinstance(proposal, Proposal) or proposal.status != 'PROPOSED': raise ValueError('invalid proposal')
        self._proposals[(proposal.tenant_scope, proposal.proposal_id)] = proposal
    def decide(self, token, tenant, proposal, status, reason, timestamp, *, row, evidence):
        principal = self.authority.resolve(token)
        if (not principal or principal[1] != tenant or not isinstance(proposal, Proposal) or proposal.tenant_scope != digest([tenant]) or proposal.status != 'PROPOSED'
                or self._proposals.get((proposal.tenant_scope, proposal.proposal_id)) is not proposal):
            raise ValueError('review denied')
        if not self._current(proposal, row, evidence): raise ValueError('stale proposal')
        if status not in ('APPROVED', 'REJECTED') or not isinstance(reason, str) or not reason.strip() or not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError('invalid decision')
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.utcoffset() is None: raise ValueError('invalid timestamp')
        except ValueError:
            raise ValueError('invalid decision') from None
        fp = digest(asdict(proposal))
        decision = Decision(proposal.proposal_id, fp, proposal.taxonomy_version, proposal.evidence_fingerprint,
                            principal[0], tenant, timestamp, status, reason)
        self.decisions.append(decision)
        self.audit.append({'proposal': proposal.proposal_id, 'reviewer': digest([principal[0]]), 'status': status})
        return decision
    @staticmethod
    def _current(proposal, row, evidence):
        return (proposal.taxonomy_version == taxonomy.TAXONOMY_VERSION
                and isinstance(evidence, Evidence) and evidence == Evidence.from_row(row)
                and proposal.entity_id == row.keyword_id and proposal.observation_id == row.observation_id
                and proposal.input_fingerprint == evidence.context_fingerprint
                and proposal.evidence_fingerprint == digest([evidence.evidence_id, evidence.context_fingerprint, evidence.version])
                and proposal.rule_version == row.classification_version
                and proposal.rule_categories == row.categories)
    def valid_for(self, decision, tenant, proposal, *, row, evidence):
        fp = digest(asdict(proposal))
        latest = next((item for item in reversed(self.decisions) if item.proposal_id == proposal.proposal_id and item.tenant == tenant), None)
        return (isinstance(decision, Decision) and self._current(proposal, row, evidence) and decision is latest and self._proposals.get((proposal.tenant_scope, proposal.proposal_id)) is proposal
                and proposal.taxonomy_version == taxonomy.TAXONOMY_VERSION and proposal.tenant_scope == digest([tenant]) and decision.tenant == tenant and decision.proposal_id == proposal.proposal_id
                and decision.proposal_fingerprint == fp and decision.taxonomy_version == proposal.taxonomy_version
                and decision.evidence_fingerprint == proposal.evidence_fingerprint)
