"""Vercel uses PostgreSQL core pins; local setup opts into legacy workflows."""

import json
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


class RuntimePackagingTests(unittest.TestCase):
    def test_requirements_delegates_to_pyproject_without_duplicate_pins(self):
        requirements = [
            line.strip()
            for line in (BACKEND / 'requirements.txt').read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')
        ]
        self.assertEqual(
            requirements,
            ['-e .[legacy-firestore,local-firestore,local-runtime]'],
        )

        with (BACKEND / 'pyproject.toml').open('rb') as manifest:
            project = tomllib.load(manifest)['project']

        self.assertEqual(project['requires-python'], '>=3.12,<3.13')
        dependencies = project['dependencies']
        legacy_firestore = project['optional-dependencies']['legacy-firestore']
        local_firestore = project['optional-dependencies']['local-firestore']
        local_runtime = project['optional-dependencies']['local-runtime']
        self.assertGreater(len(dependencies), 20)
        self.assertEqual(len(dependencies), len(set(dependencies)))
        self.assertEqual(len(legacy_firestore), len(set(legacy_firestore)))
        self.assertEqual(len(local_firestore), len(set(local_firestore)))
        self.assertEqual(len(local_runtime), len(set(local_runtime)))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in dependencies))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in legacy_firestore))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in local_firestore))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in local_runtime))
        self.assertIn('fastapi==0.162.2', dependencies)
        self.assertIn('psycopg[binary]==3.3.9', dependencies)
        self.assertIn('google-cloud-firestore==2.34.1', legacy_firestore)
        self.assertIn('protobuf==7.36.2', legacy_firestore)
        self.assertEqual(set(local_runtime), {'click==8.5.1', 'uvicorn==0.54.0'})
        for item in (
            'CacheControl==0.14.4',
            'firebase_admin==7.7.0',
            'google-cloud-storage==3.17.1',
            'google-crc32c==1.9.0',
            'google-resumable-media==2.11.0',
            'h2==4.4.1',
            'hpack==4.2.0',
            'hyperframe==6.1.0',
            'msgpack==1.2.4',
        ):
            with self.subTest(optional_dependency=item):
                self.assertIn(item, local_firestore)
                self.assertNotIn(item, dependencies)
        for item in legacy_firestore:
            with self.subTest(legacy_dependency=item):
                self.assertNotIn(item, dependencies)

    def test_postgres_runtime_import_does_not_require_legacy_cloud_packages(self):
        code = """
import importlib.abc
import sys

blocked = ('google.api_core', 'google.cloud.firestore', 'google.protobuf',
           'grpc', 'grpc_status')
class BlockLegacyCloudPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise ModuleNotFoundError('blocked legacy dependency: ' + fullname)

sys.meta_path.insert(0, BlockLegacyCloudPackages())
import app.main
import app.orchestration.vercel_postgres_live_composition
import app.storage.postgres_analysis
assert not any(name.startswith(('google.cloud.firestore', 'google.api_core',
                               'google.protobuf', 'grpc', 'grpc_status'))
               for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, '-c', code],
            cwd=BACKEND,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_vercel_function_excludes_development_sources(self):
        config = json.loads((BACKEND / 'vercel.json').read_text(encoding='utf-8'))
        patterns = config['functions']['app/main.py']['excludeFiles']
        self.assertIn('tests/**', patterns)
        self.assertIn('emulator_tests/**', patterns)
        self.assertIn('scripts/**', patterns)
