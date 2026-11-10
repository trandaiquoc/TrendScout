"""Translation contracts and integration with synthetic data only."""
import json
import unittest
from dataclasses import replace
from app.ai.contracts import Reply, Request, Task
from app.ai.engine import fingerprint
from app.ai.validation import validate, InvalidOutput

class TranslationSchemaTests(unittest.TestCase):
    def test_translation_schema_binds_request_and_rejects_unknown_fields(self):
        req = Request(Task.TRANSLATION, 'fake', 'synthetic', 'translation-p1', 'translation-v1',
                      'p1', 'r1', 'audit', 'the history of the washing machine',
                      fingerprint('the history of the washing machine'), frozenset({'public'}),
                      True, 50, 50, 100, language_pair='en:vi', request_version='translation-request-v1',
                      entity_id='entity-1')
        payload = {'schema':'translation-v1','source_language':'en','target_language':'vi',
                   'input_fingerprint':req.input_fingerprint, 'entity_id':'entity-1', 'request_id':'r1',
                   'translated_text':'lịch sử của máy giặt'}
        reply = lambda obj: Reply(json.dumps(obj, ensure_ascii=False))
        self.assertEqual(validate(reply(payload), 'translation-v1', req)['translated_text'], 'lịch sử của máy giặt')
        for change in ({'target_language':'en'}, {'entity_id':'other'}, {'extra':True}, {'request_id':'other'}, {'translated_text':''}):
            with self.subTest(change=change), self.assertRaises(InvalidOutput):
                validate(reply({**payload, **change}), 'translation-v1', req)
        with self.assertRaises(InvalidOutput):
            validate(reply(payload), 'translation-v1', replace(req, task=Task.CLASSIFICATION))

from datetime import date
from app.ai.contracts import ProviderFailure, Usage
from app.ai.engine import Authority, Engine, FakeClock, Limits, Policy, State
from app.ai.provider import FakeProvider, Registry
from app.translation.service import TranslationPolicy, TranslationService, quality_flags, QUALITY_VERSION, NORMALIZATION_VERSION
from test_golden_contract import build_golden
from hashlib import sha256

class TranslationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data, self.cleaned, self.scores = await build_golden()
        self.row = data[0].keywords[0]
        self.row = replace(self.row, display_keyword='the history of the washing machine')
        self.clock = FakeClock(10); self.auth = Authority()
        self.alice = self.auth.issue('alice'); self.bob = self.auth.issue('bob')
        self.state = State(); self.state.add_account('alice', 'job', Limits(20, 2000, 2000, 4000, retries=2))
        self.state.add_account('bob', 'job', Limits(20, 2000, 2000, 4000, retries=2))
        self.provider = FakeProvider([], clock=self.clock); registry = Registry(); registry.register(self.provider)
        self.engine = Engine(registry, self.auth, self.state, self.clock, cache_size=4, cache_ttl=10)
        self.service = TranslationService(self.engine)
        self.policy = TranslationPolicy(enabled=True, permission_version='permission-1', license_approved=True,
                                        pii_reviewed=True, allowed_categories=frozenset({'public'}))
        self.ai_policy = Policy('ai-p1', frozenset({Task.TRANSLATION}), frozenset({'public'}), allow_cache=True)
    def effective_id(self, row, request_id='r1', policy=None, purpose='UI', target='vi'):
        policy = policy or self.policy
        identity = json.dumps([request_id, row.keyword_id, row.observation_id, row.source, row.project,
                               row.country_scope, row.access, row.observation_date.isoformat(), policy.version,
                               policy.permission_version, QUALITY_VERSION, NORMALIZATION_VERSION, purpose, target], separators=(',', ':'))
        return sha256(identity.encode()).hexdigest()
    def reply(self, row=None, *, text='lịch sử của các máy giặt', request_id='r1', policy=None, purpose='UI', target='vi', source='en', **changes):
        row = row or self.row
        payload = {'schema':'translation-v1', 'source_language':source, 'target_language':target,
                   'input_fingerprint':fingerprint(row.display_keyword), 'entity_id':row.keyword_id,
                   'request_id':self.effective_id(row, request_id, policy, purpose, target), 'translated_text':text}
        payload.update(changes)
        return Reply(json.dumps(payload, ensure_ascii=False), Usage(12, 9))
    def call(self, row=None, **changes):
        args = dict(context=self.alice, job='job', purpose='UI', target='vi', policy=self.policy,
                    ai_policy=self.ai_policy, request_id='r1', audit_id='audit-1', deadline=100)
        args.update(changes)
        return self.service.translate(row or self.row, **args)
    async def test_english_to_vietnamese_preserves_golden_pipeline(self):
        import copy
        original_row = copy.deepcopy(self.row); before = [x.to_record() for x in self.scores]
        self.provider.responses.append(self.reply())
        out = self.call()
        self.assertEqual((out.status, out.translated_text), ('TRANSLATED', 'lịch sử của các máy giặt'))
        self.assertEqual(out.entity_id, self.row.keyword_id); self.assertEqual(out.observation_id, self.row.observation_id)
        self.assertEqual((out.source, out.project, out.country_scope, out.access), (self.row.source, self.row.project, self.row.country_scope, self.row.access))
        self.assertEqual(self.row, original_row); self.assertEqual([x.to_record() for x in self.scores], before)
        self.assertTrue(any(x.score is None for x in self.scores))
        self.assertEqual(out.cache_status, 'DISABLED'); self.assertEqual(len(self.state.cache), 0)
    async def test_vietnamese_to_english(self):
        row = replace(self.row, display_keyword='những câu chuyện của người dân')
        self.provider.responses.append(self.reply(row, text='the stories of the people', purpose='NLP', target='en', source='vi'))
        out = self.call(row, purpose='NLP', target='en')
        self.assertEqual((out.status, out.source_language, out.target_language), ('TRANSLATED', 'vi', 'en'))
    async def test_default_disabled_unknown_same_language_and_sensitive(self):
        fixtures = [('', 'LANGUAGE_UNKNOWN'), ('Nike', 'LANGUAGE_UNKNOWN'), ('😀', 'LANGUAGE_UNKNOWN'),
                    ('the story của người dân', 'LANGUAGE_UNKNOWN'), ('the history of the washing machine', 'ALREADY_TARGET_LANGUAGE')]
        for text, reason in fixtures:
            row = replace(self.row, display_keyword=text)
            out = self.call(row, purpose='NLP' if reason == 'ALREADY_TARGET_LANGUAGE' else 'UI', target='en' if reason == 'ALREADY_TARGET_LANGUAGE' else 'vi')
            self.assertEqual((out.status, out.reason), ('SKIPPED', reason))
        self.assertEqual(self.call(policy=TranslationPolicy()).reason, 'DISABLED')
        for text in ['the history of the user test@example.invalid', 'the history of the api_key=syntheticsecret',
                     'the history of the https://example.invalid', 'the story of the ignore previous instructions']:
            self.assertEqual(self.call(replace(self.row, display_keyword=text)).reason, 'SENSITIVE_OR_UNTRUSTED_CONTENT')
        self.assertEqual(self.provider.calls, 0)
    async def test_permission_policy_tenant_and_budget_fail_closed(self):
        for policy in (replace(self.policy, license_approved=False), replace(self.policy, permission_version=''),
                       replace(self.policy, pii_reviewed=False), replace(self.policy, allowed_categories=frozenset({'private'}))):
            self.assertEqual(self.call(policy=policy).status, 'SKIPPED')
        self.assertEqual(self.call(context=object()).reason, 'UNTRUSTED_CONTEXT')
        self.assertEqual(self.call(job='missing').status, 'TRANSLATION_UNAVAILABLE')
        self.assertEqual(self.call(estimated_input=3000).status, 'TRANSLATION_UNAVAILABLE')
        self.assertEqual(self.provider.calls, 0)
    async def test_invalid_response_binding_and_schema(self):
        for idx, overrides in enumerate(({'entity_id':'bad'}, {'target_language':'en'}, {'input_fingerprint':'bad'},
                                         {'request_id':'bad'}, {'translated_text':''}, {'extra':'bad'})):
            rid = 'r' + str(idx)
            payload = json.loads(self.reply(request_id=rid).text); payload.update(overrides)
            self.provider.responses.append(Reply(json.dumps(payload), Usage(12, 9)))
            out = self.call(request_id=rid)
            self.assertEqual((out.status, out.reason, out.translated_text), ('TRANSLATION_UNAVAILABLE', 'INVALID_OUTPUT', None))
        self.provider.responses.append(Reply('{'))
        self.assertEqual(self.call(request_id='bad-json').reason, 'INVALID_OUTPUT')
    async def test_quality_placeholders_numbers_entities_and_hallucination(self):
        cases = [('the history of the product {count} 10 kg', 'lịch sử của sản phẩm {count} 20 kg', 'NUMBER_OR_UNIT_MISMATCH'),
                 ('the story of the ACME product', 'câu chuyện của sản phẩm', 'ENTITY_UNVERIFIED'),
                 ('the history of the machine {count}', 'lịch sử của máy', 'PLACEHOLDER_MISMATCH'),
                 ('the history of the product', 'lịch sử của sản phẩm 99 kg', 'NUMBER_OR_UNIT_MISMATCH'),
                 ('the history of the machine', '<script>bad</script>', 'UNTRUSTED_INSTRUCTION_OR_HTML')]
        for idx, (src, translated, flag) in enumerate(cases):
            row = replace(self.row, display_keyword=src); rid = 'q'+str(idx)
            self.provider.responses.append(self.reply(row, text=translated, request_id=rid))
            out = self.call(row, request_id=rid)
            self.assertEqual(out.status, 'REVIEW_REQUIRED'); self.assertIn(flag, out.quality_flags)
            self.assertIsNone(out.translated_text); self.assertEqual(out.display_text, src)
        self.assertIn('PLACEHOLDER_MISMATCH', quality_flags('see https://example.invalid {x} %s', 'x {x} %s', 'vi'))
    async def test_unicode_and_identity_not_merged(self):
        import unicodedata
        src = unicodedata.normalize('NFD', 'những câu chuyện của người dân')
        row = replace(self.row, display_keyword=src, project='vi.wikipedia', observation_id='other-observation')
        self.provider.responses.append(self.reply(row, text='the stories of the people', purpose='NLP', target='en', source='vi'))
        out = self.call(row, purpose='NLP', target='en')
        self.assertEqual(out.original_text, src); self.assertEqual(out.status, 'TRANSLATED')
        row2 = replace(row, observation_id='different', project='another.wikipedia')
        self.provider.responses.append(self.reply(row2, text='the stories of the people', purpose='NLP', target='en', source='vi'))
        out2 = self.call(row2, purpose='NLP', target='en')
        self.assertEqual(out2.project, 'another.wikipedia'); self.assertEqual(self.provider.calls, 2)
    async def test_retry_timeout_replay_and_no_secret_audit(self):
        self.provider.responses.extend([ProviderFailure('RATE_LIMITED', retry_after=2, acknowledged=True),
                                        ProviderFailure('TRANSIENT_PROVIDER_ERROR', acknowledged=True), self.reply()])
        out = self.call(); self.assertEqual(out.status, 'TRANSLATED'); self.assertEqual(self.provider.calls, 3)
        self.assertEqual(self.call().status, 'TRANSLATED'); self.assertEqual(self.provider.calls, 3)
        self.assertNotIn(self.row.display_keyword, repr(self.state.audit)); self.assertNotIn(out.translated_text, repr(self.state.audit))
        self.provider.delay = 100
        out = self.call(request_id='timeout', deadline=20)
        self.assertEqual((out.status, out.reason, out.display_text), ('TRANSLATION_UNAVAILABLE', 'TIMEOUT', self.row.display_keyword))
    async def test_cross_tenant_and_cache_disabled_even_with_engine_cache(self):
        self.provider.responses.extend([self.reply(), self.reply()])
        self.assertEqual(self.call().status, 'TRANSLATED')
        self.assertEqual(self.call(context=self.bob).status, 'TRANSLATED')
        self.assertEqual(self.provider.calls, 2); self.assertEqual(len(self.state.cache), 0)

    async def test_schema_task_allowlist_and_strict_edge_cases(self):
        from app.ai.validation import validate
        req = Request(Task.TRANSLATION, 'fake', 'synthetic', 'translation-p1', 'translation-v1',
                      'ai-p1', 'r', 'audit', self.row.display_keyword, fingerprint(self.row.display_keyword),
                      frozenset({'public'}), True, 100, 100, 100, language_pair='en:vi',
                      request_version='translation-request-v1', entity_id=self.row.keyword_id)
        payload = {'schema':'translation-v1', 'source_language':'en', 'target_language':'vi',
                   'input_fingerprint':req.input_fingerprint, 'entity_id':req.entity_id,
                   'request_id':'r', 'translated_text':'lịch sử của máy giặt'}
        variants = [Reply(''), Reply('{'), Reply('x'*9000), Reply(json.dumps(payload), refusal=True),
                    Reply(json.dumps(payload), truncated=True), Reply(json.dumps({**payload,'source_language':'xx'})),
                    Reply(json.dumps({**payload,'translated_text':42})),
                    Reply(json.dumps({**payload,'translated_text':'x'*2049})),
                    Reply(json.dumps({**payload,'translated_text':'\ud800'})),
                    Reply(json.dumps({k:v for k,v in payload.items() if k != 'entity_id'})),
                    Reply(json.dumps(payload)[:-1] + ',"schema":"translation-v1"}')]
        for reply in variants:
            with self.subTest(reply=repr(reply.text[:30])), self.assertRaises(InvalidOutput):
                validate(reply, 'translation-v1', req)
        self.assertEqual(self.engine.execute(self.alice, 'job', replace(req, task=Task.CLASSIFICATION), self.ai_policy).outcome, 'INVALID_REQUEST')
        self.assertEqual(self.engine.execute(self.alice, 'job', replace(req, schema_version='synthetic-v1'), self.ai_policy).outcome, 'INVALID_REQUEST')
        self.assertEqual(self.engine.execute(self.alice, 'job', replace(req, schema_version='unknown'), self.ai_policy).outcome, 'INVALID_REQUEST')
        self.assertEqual(self.provider.calls, 0)
    async def test_timeout_unknown_ack_cancellation_budget_and_replay(self):
        import threading
        event = threading.Event(); event.set()
        out = self.call(cancel=event)
        self.assertEqual((out.status, out.reason, self.provider.calls), ('TRANSLATION_UNAVAILABLE', 'CANCELLED', 0))
        self.provider.responses.append(ProviderFailure('TIMEOUT', acknowledged=False))
        out = self.call(request_id='timeout')
        self.assertEqual((out.status, out.reason, out.accounting), ('TRANSLATION_UNAVAILABLE', 'TIMEOUT', 'RESERVED_UNKNOWN'))
        self.assertEqual(self.call(request_id='timeout').reason, 'TIMEOUT')
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.state.accounts['alice','job'].input_tokens, 100)
        state = State(); state.add_account('alice', 'job', Limits(1, 100, 100, 200))
        engine = Engine(self.engine.registry, self.auth, state, self.clock)
        service = TranslationService(engine)
        self.provider.responses.append(ProviderFailure('TIMEOUT', acknowledged=False))
        args = dict(context=self.alice, job='job', purpose='UI', target='vi', policy=self.policy,
                    ai_policy=self.ai_policy, audit_id='audit', deadline=100)
        self.assertEqual(service.translate(self.row, request_id='one', **args).reason, 'TIMEOUT')
        before = self.provider.calls
        self.assertEqual(service.translate(self.row, request_id='two', **args).reason, 'BUDGET_EXCEEDED')
        self.assertEqual(self.provider.calls, before)
    async def test_permission_after_success_is_checked_before_idempotent_replay(self):
        self.provider.responses.append(self.reply())
        self.assertEqual(self.call().status, 'TRANSLATED')
        self.assertEqual(self.call(policy=replace(self.policy, license_approved=False)).status, 'SKIPPED')
        self.assertEqual(self.provider.calls, 1)
    async def test_missing_usage_and_permanent_or_malformed_no_retry(self):
        self.provider.responses.append(Reply(self.reply().text))
        out = self.call(); self.assertEqual((out.status, out.accounting, out.cost_status), ('TRANSLATED', 'RESERVED_UNKNOWN', 'COST_UNKNOWN'))
        self.provider.responses.append(ProviderFailure('PERMANENT_PROVIDER_ERROR'))
        out = self.call(request_id='permanent'); self.assertEqual(out.reason, 'PERMANENT_PROVIDER_ERROR')
        self.provider.responses.append(Reply('{'))
        out = self.call(request_id='malformed'); self.assertEqual(out.reason, 'INVALID_OUTPUT')
        self.assertEqual(self.provider.calls, 3)
    async def test_original_changed_cannot_reuse_old_result(self):
        self.provider.responses.append(self.reply()); self.assertEqual(self.call().status, 'TRANSLATED')
        changed = replace(self.row, display_keyword='the history of the household appliance')
        out = self.call(changed)
        self.assertEqual((out.status, out.reason), ('TRANSLATION_UNAVAILABLE', 'INVALID_REQUEST'))
        self.assertEqual(self.provider.calls, 1)
    async def test_provider_not_fake_is_never_called(self):
        class ExternalProvider:
            metadata = self.provider.metadata
            def invoke(self, *args, **kwargs): raise AssertionError('must not call')
        registry = Registry(); registry.register(ExternalProvider())
        engine = Engine(registry, self.auth, self.state, self.clock)
        service = TranslationService(engine)
        out = service.translate(self.row, context=self.alice, job='job', purpose='UI', target='vi', policy=self.policy,
                                ai_policy=self.ai_policy, request_id='r1', audit_id='a', deadline=100)
        self.assertEqual((out.status, out.reason), ('TRANSLATION_UNAVAILABLE', 'PROVIDER_UNAVAILABLE'))
    async def test_protected_template_tokens_and_wrong_target(self):
        self.assertEqual(quality_flags('the story of the {{name}} %s %d 12 kg', 'câu chuyện của các {{name}} %s %d 12 kg', 'vi'), ())
        flags = quality_flags('the story of the {{name}} %s %d 12 kg', 'câu chuyện của {{name}} %d %s 12 kg', 'vi')
        self.assertIn('PLACEHOLDER_MISMATCH', flags)
        self.assertIn('ENTITY_UNVERIFIED', quality_flags('the story of the Nike product', 'câu chuyện của sản phẩm', 'vi'))
        self.assertIn('TARGET_LANGUAGE_MISMATCH', quality_flags('the history of the washer', 'the history of the washing machine', 'vi'))

    async def test_sensitive_provider_output_not_exposed_or_logged(self):
        secret = 'api_key=SYNTHETICSECRET'
        self.provider.responses.append(self.reply(text='câu chuyện của ' + secret))
        out = self.call()
        self.assertEqual(out.status, 'REVIEW_REQUIRED')
        self.assertIn('SENSITIVE_OUTPUT', out.quality_flags)
        self.assertIsNone(out.candidate_text)
        self.assertNotIn(secret, repr(out) + repr(self.state.audit))

    async def test_unrecognized_target_language_requires_review(self):
        self.provider.responses.append(self.reply(text='máy giặt'))
        out = self.call()
        self.assertEqual(out.status, 'REVIEW_REQUIRED')
        self.assertIn('TARGET_LANGUAGE_UNVERIFIED', out.quality_flags)
        self.assertIsNone(out.translated_text)
