"""Offline registry and compatibility contracts; no provider calls."""
import importlib
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from app import composition
from app.models.observations import CollectionResult
from app.sources.base import SourceMetadata
from app.sources.registry import SourceRegistry
from app.sources.wikimedia.adapter import METADATA, SOURCE, WikimediaAdapter


class FakeSource:
    async def collect(self):
        return CollectionResult(None, ())


class SourceRegistryTests(unittest.TestCase):
    def test_registration_factory_metadata_and_duplicate(self):
        registry = SourceRegistry()
        factory = Mock(return_value=FakeSource())
        meta = SourceMetadata('synthetic', 'Synthetic', 'US', 'test', 'count',
                              'Synthetic count', ('daily_snapshot',), ('timeout',))
        registry.register(meta, factory)
        self.assertEqual(factory.call_count, 0)
        self.assertIs(registry.metadata('synthetic'), meta)
        self.assertIs(registry.factory('synthetic'), factory)
        self.assertIsInstance(registry.create('synthetic'), FakeSource)
        factory.assert_called_once_with()
        with self.assertRaisesRegex(ValueError, 'already registered'):
            registry.register(meta, factory)
        self.assertEqual(factory.call_count, 1)

    def test_unknown_and_invalid_source_fails_without_construction(self):
        registry = SourceRegistry()
        factory = Mock()
        registry.register(METADATA, factory)
        for action in (registry.metadata, registry.factory, registry.create):
            with self.assertRaisesRegex(ValueError, 'Unsupported source'):
                action('not-registered')
        factory.assert_not_called()
        empty = SourceMetadata('', 'x', 'US', 'x', 'x', 'x', (), ())
        with self.assertRaisesRegex(ValueError, 'required'):
            registry.register(empty, factory)

    def test_default_registry_is_lazy_and_has_source_specific_metadata(self):
        with patch.object(composition, 'WikimediaAdapter', side_effect=AssertionError('constructed early')):
            registry = composition.source_registry()
            self.assertIs(registry.metadata(SOURCE), METADATA)
            self.assertEqual((METADATA.country_scope, METADATA.access, METADATA.metric),
                             ('US', 'all-access', 'views_ceil'))
            self.assertIn('top_list_censored', METADATA.capabilities)
            self.assertIn('WIKIMEDIA_USER_AGENT', METADATA.configuration)
            self.assertTrue(callable(registry.factory(SOURCE)))
        settings = composition.Settings.from_environment({})
        with patch.object(composition, 'WikimediaAdapter', return_value=FakeSource()) as factory:
            self.assertIsInstance(composition.source_registry(settings).create(SOURCE), FakeSource)
            factory.assert_called_once_with(settings=settings)

    def test_old_and_new_paths_are_the_same_module_and_public_symbols(self):
        old = importlib.import_module('app.adapters.wikimedia')
        new = importlib.import_module('app.sources.wikimedia.adapter')
        self.assertIs(old, new)
        for name in ('WikimediaAdapter', 'CollectionResult', 'SOURCE', 'BASE_URL',
                     'DEFAULT_USER_AGENT', 'observation_status', '_parse'):
            self.assertIs(getattr(old, name), getattr(new, name))
        self.assertIs(old.WikimediaAdapter, WikimediaAdapter)

    def test_clean_process_import_never_connects(self):
        code = '''
import socket
from unittest.mock import patch
with patch.object(socket.socket, "connect", side_effect=AssertionError("network used")):
    from app.sources.wikimedia.adapter import WikimediaAdapter
    from app import composition
    from app.adapters.wikimedia import WikimediaAdapter as old
    assert old is WikimediaAdapter
    registry = composition.source_registry()
    assert registry.factory(registry.metadata("wikimedia_pageviews_top_per_country").source_id)
'''
        env = dict(os.environ, GOOGLE_CLOUD_PROJECT='', GOOGLE_APPLICATION_CREDENTIALS='')
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
