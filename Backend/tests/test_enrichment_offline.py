"""Synthetic enrichment integration; no provider, cloud, or network access."""
import unittest
from app.ai.contracts import Request, Task, Reply
from app.ai.engine import fingerprint
from app.ai.validation import validate, InvalidOutput
from app.taxonomy.registry import TAXONOMY_VERSION

class ClassificationContractTests(unittest.TestCase):
    def test_schema_requires_evidence_and_task_binding(self):
        req = Request(Task.CLASSIFICATION, 'fake', 'synthetic', 'prompt-v1', 'classification-v1',
                      'policy', 'request', 'audit', 'the history of the fashion week',
                      fingerprint('the history of the fashion week'), frozenset({'public'}), True,
                      100, 100, 100, request_version='classification-request-v1', entity_id='e1',
                      taxonomy_version=TAXONOMY_VERSION, source_language='en', evidence_ids=('ev1',), model_version='1', allowed_categories=('fashion',))
        import json
        obj = {'schema':'classification-v1', 'entity_id':'e1', 'input_fingerprint':req.input_fingerprint,
               'request_id':'request', 'taxonomy_version':TAXONOMY_VERSION, 'source_language':'en', 'model_version':'1',
               'labels':[{'category':'fashion','subcategory':None,'evidence_id':'ev1','start':19,'end':31,'span':'fashion week','reason':'DIRECT_MENTION'}],
               'entity_type':'unknown', 'confidence':0.95}
        self.assertEqual(validate(Reply(json.dumps(obj)), 'classification-v1', req)['labels'][0]['category'], 'fashion')
        obj['labels'][0]['evidence_id'] = 'invented'
        with self.assertRaises(InvalidOutput): validate(Reply(json.dumps(obj)), 'classification-v1', req)

import copy
import json
from dataclasses import replace
from app.ai.contracts import ProviderFailure, Usage
from app.ai.engine import Authority, Engine, FakeClock, Limits, Policy, State
from app.ai.provider import FakeProvider, Registry
from app.enrichment.offline import EnrichmentPolicy, EnrichmentService, Evidence, VERSION, digest
from app.enrichment.review import ReviewerAuthority, ReviewStore
from app.enrichment.semantic import Vector, candidate
from test_golden_contract import build_golden

class EnrichmentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data, self.cleaned, self.scores = await build_golden()
        self.row = replace(data[0].keywords[0], display_keyword='the history of the fashion week', categories=('unclassified',))
        self.clock = FakeClock(10); self.auth = Authority(); self.alice = self.auth.issue('alice'); self.bob = self.auth.issue('bob')
        self.state = State(); lim = Limits(20, 2000, 2000, 4000, retries=2)
        self.state.add_account('alice','job',lim); self.state.add_account('bob','job',lim)
        self.provider = FakeProvider([], clock=self.clock); self.registry=Registry(); self.registry.register(self.provider)
        self.engine = Engine(self.registry, self.auth, self.state, self.clock, cache_size=4, cache_ttl=30)
        self.service = EnrichmentService(self.engine); self.evidence = Evidence.from_row(self.row)
        self.policy = EnrichmentPolicy(enabled=True, tasks=frozenset({Task.CLASSIFICATION, Task.NER}),
            license_approved=True, pii_reviewed=True, permission_version='permission-v1', categories=frozenset({'public'}),
            allowed_providers=frozenset({('fake','synthetic')}))
        self.ai_policy = Policy('ai-policy', frozenset({Task.CLASSIFICATION,Task.NER}), frozenset({'public'}), allow_cache=True)
    def operation(self, row, task, evidence=None, policy=None):
        evidence=evidence or Evidence.from_row(row); policy=policy or self.policy
        evfp=digest([evidence.evidence_id,evidence.context_fingerprint,evidence.version])
        return digest([VERSION,task.value,row.keyword_id,row.observation_id,row.source,row.project,row.country_scope,row.access,
                       row.observation_date.isoformat(),fingerprint(row.display_keyword),evfp,TAXONOMY_VERSION,
                       row.classification_version,policy.version,policy.permission_version,'fake','synthetic'])
    def reply(self, row=None, task=Task.CLASSIFICATION, *, labels=None, mentions=None, policy=None, **changes):
        row=row or self.row; evidence=Evidence.from_row(row); schema='classification-v1' if task is Task.CLASSIFICATION else 'ner-v1'
        obj={'schema':schema,'entity_id':row.keyword_id,'input_fingerprint':fingerprint(row.display_keyword),
             'request_id':self.operation(row,task,evidence,policy),'taxonomy_version':TAXONOMY_VERSION,
             'source_language':'en','model_version':'1'}
        if task is Task.CLASSIFICATION:
            obj.update(labels=labels if labels is not None else [self.label(row,'fashion','fashion week')], entity_type='unknown',confidence=0.95)
        else:
            obj.update(mentions=mentions if mentions is not None else [self.mention(row,'fashion week')])
        obj.update(changes); return Reply(json.dumps(obj,ensure_ascii=False),Usage(20,10))
    def label(self,row,category,span,subcategory=None,reason='DIRECT_MENTION'):
        start=row.display_keyword.index(span); return {'category':category,'subcategory':subcategory,
            'evidence_id':Evidence.from_row(row).evidence_id,'start':start,'end':start+len(span),'span':span,'reason':reason}
    def mention(self,row,span,kind='event'):
        start=row.display_keyword.index(span); return {'entity_type':kind,'evidence_id':Evidence.from_row(row).evidence_id,
            'start':start,'end':start+len(span),'span':span}
    def call(self,row=None,task=Task.CLASSIFICATION,**kwargs):
        row=row or self.row
        args=dict(task=task,context=self.alice,job='job',evidence=Evidence.from_row(row),policy=self.policy,
                  ai_policy=self.ai_policy,audit_id='audit',deadline=100)
        args.update(kwargs); return self.service.propose(row,**args)
    async def test_golden_unclassified_classification_and_ner_no_core_change(self):
        before=copy.deepcopy(self.row); scores=[x.to_record() for x in self.scores]
        self.provider.responses.append(self.reply()); out=self.call()
        self.assertEqual((out.status,out.review_status,out.rule_categories),('PROPOSED','REVIEW_REQUIRED',('unclassified',)))
        self.assertEqual((out.model_confidence,out.calibration,out.commercial_relevance),(0.95,'CALIBRATION_NOT_EVALUATED','not_assessed'))
        self.provider.responses.append(self.reply(task=Task.NER)); ner=self.call(task=Task.NER)
        self.assertEqual((ner.status,ner.review_status),('PROPOSED','REVIEW_REQUIRED'))
        self.assertEqual(self.row,before); self.assertEqual([x.to_record() for x in self.scores],scores)
        self.assertTrue(any(x.score is None for x in self.scores)); self.assertEqual(len(self.state.cache),0)
    async def test_rule_classified_remains_unchanged_multi_label(self):
        row=replace(self.row,display_keyword='the fashion week and the film festival',categories=('fashion',))
        policy=replace(self.policy,reviewer_requested=True)
        labels=[self.label(row,'fashion','fashion week'),self.label(row,'entertainment','film','entertainment.film')]
        self.provider.responses.append(self.reply(row,labels=labels,policy=policy)); out=self.call(row,policy=policy)
        self.assertEqual((out.status,out.rule_categories,len(out.labels)),('PROPOSED',('fashion',),2))
        self.assertEqual(row.categories,('fashion',))
    async def test_invalid_evidence_wrong_entity_offsets_span_and_confidence(self):
        variants=[{'evidence_id':'fake'}, {'start':0}, {'span':'wrong'}, {'end':999}]
        for i, change in enumerate(variants):
            row=replace(self.row, observation_id='obs-'+str(i)); label=self.label(row,'fashion','fashion week'); label.update(change)
            self.provider.responses.append(self.reply(row,labels=[label])); out=self.call(row)
            self.assertEqual(out.status,'REJECTED'); self.assertEqual(out.labels,())
        wrong=Evidence.from_row(replace(self.row,observation_id='other'))
        self.assertEqual(self.call(evidence=wrong).reason,'INVALID_EVIDENCE')
    async def test_invalid_taxonomy_duplicate_hierarchy_and_binding(self):
        variants=[{'category':'invented'}, {'subcategory':'sports.teams'}, {'reason':'bad'}]
        for i, change in enumerate(variants):
            row=replace(self.row,observation_id='tax-'+str(i)); label=self.label(row,'fashion','fashion week'); label.update(change)
            self.provider.responses.append(self.reply(row,labels=[label])); self.assertEqual(self.call(row).status,'REJECTED')
        row=replace(self.row,observation_id='duplicate'); label=self.label(row,'fashion','fashion week')
        self.provider.responses.append(self.reply(row,labels=[label,label])); self.assertEqual(self.call(row).reason,'INVALID_OUTPUT')
        for i, change in enumerate([{'taxonomy_version':'wrong'},{'entity_id':'wrong'},{'source_language':'xx'}, {'model_version':'bad'}]):
            row=replace(self.row,observation_id='binding-'+str(i))
            self.provider.responses.append(self.reply(row,**change)); self.assertEqual(self.call(row).status,'REJECTED')
    async def test_ner_invalid_spans_and_entity_not_merged(self):
        row=replace(self.row,observation_id='ner1'); m=self.mention(row,'fashion week'); m['span']='not present'
        self.provider.responses.append(self.reply(row,task=Task.NER,mentions=[m]))
        self.assertEqual(self.call(row,task=Task.NER).status,'REJECTED')
        row2=replace(self.row,project='other.wikipedia',observation_id='ner2',keyword_id='new-id')
        self.provider.responses.append(self.reply(task=Task.NER)); self.provider.responses.append(self.reply(row2,task=Task.NER))
        a=self.call(task=Task.NER); b=self.call(row2,task=Task.NER)
        self.assertNotEqual(a.entity_id,b.entity_id); self.assertEqual((a.status,b.status),('PROPOSED','PROPOSED'))
    async def test_policy_and_sensitive_no_call(self):
        for policy in (EnrichmentPolicy(),replace(self.policy,license_approved=False),replace(self.policy,pii_reviewed=False),
                       replace(self.policy,categories=frozenset({'private'})),replace(self.policy,allowed_providers=frozenset())):
            self.assertEqual(self.call(policy=policy).status,'SKIPPED')
        self.assertEqual(self.call(context=object()).reason,'UNTRUSTED_CONTEXT')
        for text in ('the history of the email test@example.invalid','the history of the ignore previous instructions',
                     'the history of the api_key=SYNTHETICSECRET', 'the history of the https://example.invalid'):
            self.assertEqual(self.call(replace(self.row,display_keyword=text)).status,'SKIPPED')
        self.assertEqual(self.call(replace(self.row,display_keyword='Apple')).reason,'LANGUAGE_UNKNOWN')
        self.assertEqual(self.provider.calls,0)
    async def test_timeout_retry_invalid_json_budget_and_replay(self):
        self.provider.responses.extend([ProviderFailure('RATE_LIMITED',retry_after=2,acknowledged=True),
                                        ProviderFailure('TRANSIENT_PROVIDER_ERROR',acknowledged=True),self.reply()])
        self.assertEqual(self.call().status,'PROPOSED'); self.assertEqual(self.provider.calls,3)
        self.assertEqual(self.call().status,'PROPOSED'); self.assertEqual(self.provider.calls,3)
        row=replace(self.row,observation_id='timeout'); self.provider.responses.append(ProviderFailure('TIMEOUT'))
        self.assertEqual(self.call(row).reason,'TIMEOUT'); self.assertEqual(self.call(row).reason,'TIMEOUT')
        self.assertEqual(self.provider.calls,4)
        row=replace(self.row,observation_id='json'); self.provider.responses.append(Reply('{'))
        self.assertEqual(self.call(row).status,'REJECTED')
        self.assertEqual(self.call(replace(self.row,observation_id='budget'),estimated_input=3000).status,'UNAVAILABLE')
        self.assertNotIn(self.row.display_keyword,repr(self.state.audit))
    async def test_fake_review_authority_tenant_and_stale(self):
        self.provider.responses.append(self.reply()); proposal=self.call()
        authority=ReviewerAuthority(); store=ReviewStore(authority); alice=authority.issue('reviewer','alice'); bob=authority.issue('reviewer2','bob')
        with self.assertRaises(ValueError): store.decide(alice,'alice',proposal,'APPROVED','checked','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        store.register_trusted(proposal)
        with self.assertRaises(ValueError): store.decide(bob,'bob',proposal,'APPROVED','checked','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        with self.assertRaises(ValueError): store.decide(object(),'alice',proposal,'APPROVED','checked','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        approved=store.decide(alice,'alice',proposal,'APPROVED','checked','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        self.assertTrue(store.valid_for(approved,'alice',proposal,row=self.row,evidence=self.evidence)); self.assertFalse(store.valid_for(approved,'bob',proposal,row=self.row,evidence=self.evidence))
        self.assertFalse(store.valid_for(approved,'alice',replace(proposal,taxonomy_version='new'),row=self.row,evidence=self.evidence))
        self.assertFalse(store.valid_for(approved,'alice',replace(proposal,input_fingerprint='changed'),row=self.row,evidence=self.evidence))
        rejected=store.decide(alice,'alice',proposal,'REJECTED','unclear','2026-01-02T00:00:00+00:00',row=self.row,evidence=self.evidence)
        self.assertEqual(rejected.status,'REJECTED'); self.assertFalse(store.valid_for(approved,'alice',proposal,row=self.row,evidence=self.evidence)); self.assertEqual(self.row.categories,('unclassified',))
    async def test_semantic_candidate_scope_and_vector_guards(self):
        row2=replace(self.row,keyword_id='other',observation_id='other',display_keyword='the history of the garment show')
        vec=Vector((1.,0.),'synthetic-v1','alice',self.row.source,self.row.country_scope,self.row.access,self.row.observation_date.isoformat())
        out,status=candidate(self.row,row2,vec,vec,authority=self.auth,context=self.alice)
        self.assertEqual((status,out.review_status,out.similarity),('CANDIDATE_ONLY','REVIEW_REQUIRED',1.0))
        for other, expected in [(None,'NO_SEMANTIC_EVIDENCE'),(replace(vec,values=(1.,0.,2.)),'DIMENSION_MISMATCH'),
             (replace(vec,model_version='other'),'MODEL_MISMATCH'),(replace(vec,tenant='bob'),'INCOMPATIBLE_SCOPE'),
             (replace(vec,values=(0.,0.)),'INVALID_VECTOR'),(replace(vec,values=(float('nan'),0.)),'INVALID_VECTOR')]:
            self.assertEqual(candidate(self.row,row2,vec,other,authority=self.auth,context=self.alice)[1],expected)
    async def test_translation_review_unavailable_or_changed_binding_never_becomes_evidence(self):
        from app.translation.service import Sidecar
        trans=Sidecar(self.row.keyword_id,self.row.observation_id,self.row.display_keyword,fingerprint(self.row.display_keyword),
                      status='REVIEW_REQUIRED',candidate_text='unverified')
        self.provider.responses.append(self.reply()); out=self.call(translation=trans)
        self.assertEqual(out.status,'PROPOSED')  # original English used; candidate never sent
        self.assertEqual(self.call(translation=replace(trans,original_fingerprint='wrong')).reason,'TRANSLATION_BINDING_MISMATCH')
        self.assertEqual(self.call(translation=replace(trans,schema_version='wrong')).reason,'TRANSLATION_BINDING_MISMATCH')
        self.assertEqual(self.call(replace(self.row,display_keyword='inconnu'),translation=trans).reason,'LANGUAGE_UNKNOWN')

    async def test_schema_adversarial_matrix_through_engine(self):
        label=self.label(self.row,'fashion','fashion week')
        bad_labels=[[label,self.label(self.row,'unclassified','fashion week')],
                    [self.label(self.row,'fashion','fashion week','entertainment.film')],
                    [dict(label,evidence_id='invented')], [label]*9]
        for i, labels in enumerate(bad_labels):
            row=replace(self.row,observation_id='schema-label-'+str(i))
            labels=[dict(item,evidence_id=Evidence.from_row(row).evidence_id) if item['evidence_id']!='invented' else item for item in labels]
            self.provider.responses.append(self.reply(row,labels=labels))
            self.assertEqual(self.call(row).status,'REJECTED')
        for i, changes in enumerate(({'review_status':'APPROVED'}, {'confidence':True}, {'confidence':1.2},
                                     {'entity_type':'superhuman'}, {'schema':'translation-v1'})):
            row=replace(self.row,observation_id='schema-field-'+str(i))
            self.provider.responses.append(self.reply(row,**changes))
            self.assertEqual(self.call(row).status,'REJECTED')
        row=replace(self.row,observation_id='duplicate-json')
        raw=self.reply(row).text[:-1]+',"schema":"classification-v1"}'
        self.provider.responses.append(Reply(raw)); self.assertEqual(self.call(row).status,'REJECTED')
    async def test_scope_evidence_other_project_date_rejected_and_same_title_not_merged(self):
        for changed in [replace(self.row,project='vi.wikipedia'), replace(self.row,observation_id='different'),
                        replace(self.row,country_scope='GB')]:
            self.assertEqual(self.call(evidence=Evidence.from_row(changed)).reason,'INVALID_EVIDENCE')
        self.provider.responses.extend([self.reply(), self.reply()])
        a=self.call(); b=self.call(context=self.bob)
        self.assertEqual((a.status,b.status),('PROPOSED','PROPOSED'))
        self.assertNotEqual(a.tenant_scope,b.tenant_scope); self.assertEqual(self.provider.calls,2)
    async def test_language_translation_and_safe_disabled_behavior(self):
        from app.translation.service import Sidecar
        unknown=replace(self.row,display_keyword='the story của người dân')
        self.assertEqual(self.call(unknown).reason,'LANGUAGE_UNKNOWN')
        translation=Sidecar(self.row.keyword_id,self.row.observation_id,self.row.display_keyword,
                            fingerprint(self.row.display_keyword),status='TRANSLATION_UNAVAILABLE')
        self.provider.responses.append(self.reply()); out=self.call(translation=translation)
        self.assertEqual(out.status,'PROPOSED')
        self.assertEqual(self.call(translation=replace(translation,entity_id='wrong')).reason,'TRANSLATION_BINDING_MISMATCH')
    async def test_semantic_cross_tenant_and_short_title_fail_closed(self):
        row2=replace(self.row,keyword_id='new',display_keyword='the history of the washer')
        vec=Vector((1.,0.),'v1','alice',self.row.source,self.row.country_scope,self.row.access,self.row.observation_date.isoformat())
        self.assertEqual(candidate(self.row,row2,vec,vec,authority=self.auth,context=self.bob)[1],'INCOMPATIBLE_SCOPE')
        self.assertEqual(candidate(self.row,row2,vec,vec,authority=self.auth,context=object())[1],'UNTRUSTED_CONTEXT')
        self.assertEqual(candidate(self.row,replace(row2,display_keyword='short'),vec,vec,authority=self.auth,context=self.alice)[1],'SHORT_TITLE')
    async def test_ai_cannot_mint_approval_and_review_fingerprint_immutable(self):
        self.provider.responses.append(self.reply()); p=self.call()
        authority=ReviewerAuthority(); reviewer=authority.issue('reviewer','alice'); store=ReviewStore(authority)
        store.register_trusted(p)
        with self.assertRaises(ValueError): store.decide(reviewer,'alice',replace(p,review_status='APPROVED'),'APPROVED','checked','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        decision=store.decide(reviewer,'alice',p,'APPROVED','checked','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        self.assertFalse(store.valid_for(decision,'alice',replace(p,evidence_fingerprint='changed'),row=self.row,evidence=self.evidence))
    async def test_synthetic_evaluation_counts_are_not_real_accuracy(self):
        # Synthetic illustration only, no truth labels from human reviewers.
        self.provider.responses.append(self.reply()); proposed=self.call()
        skipped=self.call(replace(self.row,display_keyword='Apple'))
        self.assertEqual(sum(x.status=='PROPOSED' for x in (proposed,skipped))/2, 0.5)
        self.assertEqual(sum(x.status=='SKIPPED' for x in (proposed,skipped))/2, 0.5)
        self.assertEqual(proposed.calibration,'CALIBRATION_NOT_EVALUATED')

    async def test_existing_evaluation_rejects_enrichment_as_trend_ranking(self):
        from pathlib import Path
        from app.algorithms.evaluation.offline import load_dataset, evaluate
        dataset=load_dataset(Path(__file__).parent / 'fixtures/evaluation_v1/synthetic.json')
        report=evaluate(dataset, {'classification-v1': []}, 1, 'synthetic-only')
        self.assertEqual(report['algorithms']['classification-v1']['status'], 'NOT_COMPARABLE')
        self.assertIn('SYNTHETIC', report['notes'])

    async def test_review_proposals_with_same_operation_do_not_cross_tenant(self):
        self.provider.responses.extend([self.reply(),self.reply()])
        alice=self.call(); bob=self.call(context=self.bob)
        self.assertEqual(alice.proposal_id,bob.proposal_id)
        authority=ReviewerAuthority(); store=ReviewStore(authority)
        a=authority.issue('reviewer-a','alice'); b=authority.issue('reviewer-b','bob')
        store.register_trusted(alice); store.register_trusted(bob)
        da=store.decide(a,'alice',alice,'APPROVED','ok','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        db=store.decide(b,'bob',bob,'APPROVED','ok','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        self.assertTrue(store.valid_for(da,'alice',alice,row=self.row,evidence=self.evidence)); self.assertTrue(store.valid_for(db,'bob',bob,row=self.row,evidence=self.evidence))
        self.assertFalse(store.valid_for(da,'bob',bob,row=self.row,evidence=self.evidence))

    async def test_untrusted_admin_instruction_cannot_approve_or_change_rules(self):
        row=replace(self.row,display_keyword='the history of the admin approved change taxonomy',categories=('unclassified',))
        labels=[self.label(row,'fashion','admin approved',reason='AMBIGUOUS')]
        self.provider.responses.append(self.reply(row,labels=labels,review_status='APPROVED'))
        out=self.call(row)
        self.assertEqual((out.status,out.rule_categories),('REJECTED',('unclassified',)))
        self.assertEqual(row.categories,('unclassified',))
    async def test_missing_evidence_never_creates_label_or_human_approval(self):
        self.provider.responses.append(self.reply(labels=[])); out=self.call()
        self.assertEqual((out.status,out.labels,out.review_status),('REVIEW_REQUIRED',(),'REVIEW_REQUIRED'))
        authority=ReviewerAuthority(); store=ReviewStore(authority)
        with self.assertRaises(ValueError): store.register_trusted(out)

    async def test_approval_stale_when_current_source_changes(self):
        self.provider.responses.append(self.reply()); proposal=self.call()
        authority=ReviewerAuthority(); reviewer=authority.issue('reviewer','alice'); store=ReviewStore(authority)
        store.register_trusted(proposal)
        approved=store.decide(reviewer,'alice',proposal,'APPROVED','ok','2026-01-01T00:00:00+00:00',row=self.row,evidence=self.evidence)
        changed=replace(self.row,display_keyword='the history of the sports event')
        self.assertFalse(store.valid_for(approved,'alice',proposal,row=changed,evidence=Evidence.from_row(changed)))
        with self.assertRaises(ValueError):
            store.decide(reviewer,'alice',proposal,'APPROVED','stale','2026-01-01T00:00:00+00:00',row=changed,evidence=Evidence.from_row(changed))

    async def test_irrelevant_but_matching_evidence_is_not_a_verified_fact(self):
        # A matching text span alone cannot prove the semantic inference.
        label=self.label(self.row,'technology','fashion week',reason='AMBIGUOUS')
        self.provider.responses.append(self.reply(labels=[label],confidence=0.99))
        out=self.call()
        self.assertEqual((out.status,out.review_status),('PROPOSED','REVIEW_REQUIRED'))
        self.assertIn('EVIDENCE_IS_NOT_INFERENCE_PROOF',out.quality_flags)
        self.assertIn('AMBIGUOUS',out.quality_flags)
        self.assertEqual(self.row.categories,('unclassified',))
