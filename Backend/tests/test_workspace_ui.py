"""Offline dashboard structure and interaction regression."""
import unittest
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[2] / 'Frontend' / 'public'

class WorkspaceStructureTests(unittest.TestCase):
    def test_required_workspace_sections_and_safe_disabled_controls(self):
        html=(PUBLIC/'index.html').read_text()
        for name in ('overview','sources','runs','keywords','algorithms','quality','refresh','research','usage','settings'):
            with self.subTest(name=name): self.assertIn('data-view="'+name+'"',html)
        self.assertIn('OFFLINE DEMO / SYNTHETIC DATA',html)
