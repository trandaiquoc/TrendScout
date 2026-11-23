"""Vercel uses core runtime pins; local setup adds its Firebase Admin adapter."""

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
        self.assertEqual(requirements, ['-e .[local-firestore]'])

        with (BACKEND / 'pyproject.toml').open('rb') as manifest:
            project = tomllib.load(manifest)['project']

        self.assertEqual(project['requires-python'], '>=3.12,<3.13')
        dependencies = project['dependencies']
        local_firestore = project['optional-dependencies']['local-firestore']
        self.assertGreater(len(dependencies), 25)
        self.assertEqual(len(dependencies), len(set(dependencies)))
        self.assertEqual(len(local_firestore), len(set(local_firestore)))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in dependencies))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in local_firestore))
        self.assertIn('fastapi==0.162.2', dependencies)
        self.assertIn('google-cloud-firestore==2.34.1', dependencies)
        self.assertIn('uvicorn==0.54.0', dependencies)
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

    def test_vercel_app_import_does_not_load_firebase_admin(self):
        result = subprocess.run(
            [sys.executable, '-c',
             "import sys; import app.main; assert 'firebase_admin' not in sys.modules"],
            cwd=BACKEND,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
