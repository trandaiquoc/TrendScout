"""Exercise the frontend's local-live capability and provenance gates offline."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DashboardCapabilityTests(unittest.TestCase):
    def test_local_live_gate_and_discovery_modes(self):
        script = """
import assert from 'node:assert/strict';
import {canStartFetch, acceptsDiscovery} from './Frontend/public/modules/capabilities.js';
const base = {public_fetch_enabled: true, fetch_enabled: true, authentication_required: false,
  environment_mode: 'local', read_only: false, local_live_demo: true, data_mode: 'LIVE',
  sources: [{enabled_for_current_operation: true}]};
assert.equal(canStartFetch(base), true);
assert.equal(acceptsDiscovery(base, {data_mode:'LIVE'}), true);
assert.equal(acceptsDiscovery(base, {data_mode:'MIXED'}), true);
assert.equal(acceptsDiscovery(base, {data_mode:'SYNTHETIC'}), false);
for (const changes of [{environment_mode:'vercel'}, {read_only:true},
  {local_live_demo:false}, {fetch_enabled:false}]) {
  assert.equal(canStartFetch({...base, ...changes}), false);
}
const synthetic = {...base, data_mode:'SYNTHETIC', local_live_demo:false};
assert.equal(canStartFetch(synthetic), true);
assert.equal(acceptsDiscovery(synthetic, {data_mode:'LIVE'}), false);
assert.equal(acceptsDiscovery(synthetic, {data_mode:'SYNTHETIC'}), true);
"""
        subprocess.run(["node", "--input-type=module", "-e", script],
                       cwd=ROOT, check=True, timeout=10, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
