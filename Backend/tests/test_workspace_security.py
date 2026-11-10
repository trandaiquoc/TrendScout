"""Fake, process-local authorization; no API endpoints or production RBAC."""
import unittest
from app.security_preview.offline import FakeAuthority, FakeSecurityService, Resource, SecretMetadata

class WorkspaceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.auth=FakeAuthority(); self.service=FakeSecurityService(self.auth)
        for kind in ('dataset','research','job','budget','settings','audit','review'):
            self.service.register_trusted(Resource('a-'+kind,kind,'a',frozenset({'b'}) if kind=='dataset' else frozenset()))
        self.viewer=self.auth.issue('view','a','viewer'); self.operator=self.auth.issue('op','a','operator')
        self.reviewer=self.auth.issue('review','a','reviewer'); self.analyst_b=self.auth.issue('b','b','analyst')
    def test_role_and_tenant_scope_are_server_owned(self):
        self.assertTrue(self.service.authorize(self.viewer,'dataset.read','a-dataset'))
        self.assertFalse(self.service.authorize(self.viewer,'job.run','a-job'))
        self.assertTrue(self.service.authorize(self.operator,'job.run','a-job'))
        self.assertFalse(self.service.authorize(self.analyst_b,'research.read','a-research'))
        self.assertFalse(self.service.authorize(self.analyst_b,'budget.read','a-budget'))
        self.assertFalse(self.service.authorize(self.analyst_b,'settings.write','a-settings'))
        self.assertFalse(self.service.authorize(self.analyst_b,'review.write','a-review'))
        self.assertTrue(self.service.authorize(self.analyst_b,'dataset.read','a-dataset'))
        self.assertFalse(self.service.authorize(object(),'dataset.read','a-dataset'))
        self.assertEqual(self.service.audit[-1]['outcome'],'DENY')
    def test_unsafe_share_and_secret_metadata_are_rejected(self):
        with self.assertRaises(ValueError): self.service.register_trusted(Resource('private','research','a',frozenset({'b'})))
        with self.assertRaises(ValueError): SecretMetadata('fake-ref-a','SYNTHETIC','RAW-SECRET','a','SYNTHETIC_ONLY')
        self.assertEqual(SecretMetadata('fake-ref-a','SYNTHETIC','••••1234','a','SYNTHETIC_ONLY').status,'SYNTHETIC_ONLY')
    def test_role_matrix_and_resource_kind(self):
        expected={'viewer':set(), 'analyst':{'research.write'}, 'operator':{'job.run'},
                  'reviewer':{'review.write'}, 'tenant_admin':{'research.write','job.run','settings.write'},
                  'platform_admin':set()}
        resources={'research.write':'a-research','job.run':'a-job','review.write':'a-review','settings.write':'a-settings'}
        for role,allowed in expected.items():
            principal=self.auth.issue('member-'+role,'a',role)
            for action,resource in resources.items():
                with self.subTest(role=role,action=action):
                    self.assertEqual(self.service.authorize(principal,action,resource),action in allowed)
            self.assertFalse(self.service.authorize(principal,'job.run','a-research'))
    def test_untrusted_role_and_unknown_principal_cannot_operate(self):
        with self.assertRaises(ValueError): self.auth.issue('impersonator','b','root')
        for token in (object(),{'tenant':'a','role':'tenant_admin'},None):
            with self.assertRaises(PermissionError): self.service.perform(token,'job.run','a-job')
        self.assertTrue(all(entry['outcome']=='DENY' for entry in self.service.audit))
    def test_secret_contract_rejects_raw_values_and_timestamp_smuggling(self):
        for kwargs in ({'reference_id':'real-secret'}, {'provider':'RAWSECRET'}, {'masked_label':'••••key!'},
                       {'created_at':'SECRET-FAKE'}, {'tenant':'RAW_SECRET_WITH_SPACES'}):
            base={'reference_id':'fake-ref-1','provider':'SYNTHETIC','masked_label':'••••1234','tenant':'a','status':'SYNTHETIC_ONLY'}
            base.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): SecretMetadata(**base)
    def test_private_review_and_audit_are_not_shared_with_dataset(self):
        reviewer_b=self.auth.issue('reviewer-b','b','reviewer')
        self.assertTrue(self.service.authorize(reviewer_b,'dataset.read','a-dataset'))
        self.assertFalse(self.service.authorize(reviewer_b,'review.write','a-review'))
        self.assertFalse(self.service.authorize(reviewer_b,'audit.read','a-audit'))
    def test_authorized_role_still_cannot_cross_tenant(self):
        operator_b=self.auth.issue('operator-b','b','operator')
        self.service.register_trusted(Resource('b-budget','budget','b'))
        self.service.register_trusted(Resource('b-job','job','b'))
        self.assertTrue(self.service.authorize(operator_b,'budget.read','b-budget'))
        self.assertTrue(self.service.authorize(operator_b,'job.run','b-job'))
        self.assertFalse(self.service.authorize(operator_b,'budget.read','a-budget'))
        self.assertFalse(self.service.authorize(operator_b,'job.run','a-job'))

    def test_expired_fake_identity_fails_closed(self):
        self.assertTrue(self.service.authorize(self.operator,'job.run','a-job'))
        self.auth.expire(self.operator)
        self.assertFalse(self.service.authorize(self.operator,'job.run','a-job'))
        self.assertEqual(self.service.audit[-1]['outcome'],'DENY')
