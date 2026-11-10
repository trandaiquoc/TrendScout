"""Proposed in-process authorization contracts; never wired to demo routes."""
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re

ROLE_PERMISSIONS = {
    'viewer': frozenset({'dataset.read'}),
    'analyst': frozenset({'dataset.read', 'research.read', 'research.write'}),
    'operator': frozenset({'dataset.read', 'job.run', 'budget.read'}),
    'reviewer': frozenset({'dataset.read', 'research.read', 'review.write', 'audit.read'}),
    'tenant_admin': frozenset({'dataset.read', 'research.read', 'research.write', 'job.run', 'budget.read', 'settings.write', 'audit.read'}),
    'platform_admin': frozenset({'dataset.read'}),
}
RESOURCE_ACTIONS = {'dataset.read':'dataset', 'research.read':'research', 'research.write':'research',
                    'job.run':'job', 'budget.read':'budget', 'settings.write':'settings',
                    'audit.read':'audit', 'review.write':'review'}

@dataclass(frozen=True)
class Principal:
    identity: str
    tenant: str
    role: str

@dataclass(frozen=True)
class Resource:
    resource_id: str
    kind: str
    tenant: str
    shared_tenants: frozenset[str] = frozenset()

@dataclass(frozen=True)
class SecretMetadata:
    reference_id: str
    provider: str
    masked_label: str
    tenant: str
    status: str
    created_at: str | None = None
    rotated_at: str | None = None
    def __post_init__(self):
        if (not re.fullmatch(r'fake-ref-[a-z0-9-]{1,32}', self.reference_id)
                or self.provider not in ('UNCONFIGURED', 'SYNTHETIC')
                or not re.fullmatch(r'(?:UNCONFIGURED|[•*]{4}[0-9]{4})',self.masked_label)
                or self.status not in ('UNCONFIGURED','SYNTHETIC_ONLY')
                or not re.fullmatch(r'[a-z0-9_-]{1,32}',self.tenant)):
            raise ValueError('invalid metadata')
        for timestamp in (self.created_at,self.rotated_at):
            if timestamp is not None:
                try:
                    if datetime.fromisoformat(timestamp).utcoffset() is None: raise ValueError()
                except (ValueError,TypeError): raise ValueError('invalid metadata') from None

class FakeAuthority:
    """Injected test tokens model identity + membership, not real authentication."""
    def __init__(self): self._principals = {}
    def issue(self, identity, tenant, role):
        if not identity or not tenant or role not in ROLE_PERMISSIONS: raise ValueError('invalid membership')
        token=object(); self._principals[token]=Principal(identity,tenant,role); return token
    def expire(self, token):
        self._principals.pop(token, None)
    def resolve(self, token):
        try: return self._principals.get(token)
        except TypeError: return None

class FakeSecurityService:
    def __init__(self, authority):
        self.authority=authority; self.resources={}; self.audit=[]
    def register_trusted(self, resource):
        if not isinstance(resource,Resource) or resource.kind not in set(RESOURCE_ACTIONS.values()) or not resource.tenant or not resource.resource_id:
            raise ValueError('invalid resource')
        if resource.shared_tenants and resource.kind != 'dataset': raise ValueError('private resource cannot be shared')
        if resource.resource_id in self.resources: raise ValueError('duplicate resource')
        self.resources[resource.resource_id]=resource
    def authorize(self, token, action, resource_id):
        principal=self.authority.resolve(token); resource=self.resources.get(resource_id)
        allowed=bool(principal and resource and action in ROLE_PERMISSIONS[principal.role]
                     and RESOURCE_ACTIONS.get(action)==resource.kind
                     and (resource.tenant==principal.tenant or (resource.kind=='dataset' and action=='dataset.read' and principal.tenant in resource.shared_tenants)))
        self.audit.append({'actor':sha256(principal.identity.encode()).hexdigest()[:16] if principal else 'UNAUTHENTICATED',
                           'tenant':principal.tenant if principal else None,
                           'action':action if action in RESOURCE_ACTIONS else 'UNKNOWN',
                           'outcome':'ALLOW' if allowed else 'DENY'})
        return allowed
    def perform(self, token, action, resource_id):
        if not self.authorize(token,action,resource_id): raise PermissionError('DENIED')
        return 'SYNTHETIC_ALLOWED'
