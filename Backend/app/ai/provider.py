"""Explicit registry and deterministic synthetic provider; no networking."""
from collections import deque
from .contracts import Metadata, Task, Reply, ProviderFailure

class Registry:
    def __init__(self):
        self._providers = {}
    def register(self, provider):
        key = provider.metadata.provider_id
        if key in self._providers:
            raise ValueError('duplicate provider')
        self._providers[key] = provider
    def get(self, provider_id, model_id, task):
        provider = self._providers.get(provider_id)
        if provider is None or provider.metadata.model_id != model_id:
            raise ProviderFailure('PROVIDER_UNAVAILABLE')
        if task not in provider.metadata.tasks:
            raise ProviderFailure('PROVIDER_UNAVAILABLE')
        return provider

FAKE_METADATA = Metadata('fake', '1', 'synthetic', '1', frozenset(Task), frozenset({'strict-json'}),
                         4096, 1024, True, True, True,
                         frozenset({'TIMEOUT','RATE_LIMITED','TRANSIENT_PROVIDER_ERROR','PERMANENT_PROVIDER_ERROR','CANCELLED'}))

class FakeProvider:
    def __init__(self, responses, *, clock=None, delay=0):
        self.metadata = FAKE_METADATA
        self.responses = deque(responses)
        self.calls = 0
        self.clock = clock
        self.delay = delay
    def invoke(self, instructions, source_data, *, timeout, max_output_tokens):
        self.calls += 1
        if self.clock and self.delay:
            self.clock.advance(min(self.delay, timeout))
        if self.delay > timeout:
            raise ProviderFailure('TIMEOUT', acknowledged=False)
        if not self.responses:
            raise ProviderFailure('PERMANENT_PROVIDER_ERROR')
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response
