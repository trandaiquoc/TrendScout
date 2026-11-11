"""Runtime dependencies have one pinned source of truth for local and Vercel installs."""

import re
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
        self.assertEqual(requirements, ['-e .'])

        with (BACKEND / 'pyproject.toml').open('rb') as manifest:
            project = tomllib.load(manifest)['project']

        self.assertEqual(project['requires-python'], '>=3.12,<3.13')
        dependencies = project['dependencies']
        self.assertGreater(len(dependencies), 30)
        self.assertEqual(len(dependencies), len(set(dependencies)))
        self.assertTrue(all(re.fullmatch(r'[^=<>!~]+==[^=]+', item) for item in dependencies))
        self.assertIn('fastapi==0.162.2', dependencies)
        self.assertIn('firebase_admin==7.7.0', dependencies)
        self.assertIn('google-cloud-firestore==2.34.1', dependencies)
        self.assertIn('uvicorn==0.54.0', dependencies)
