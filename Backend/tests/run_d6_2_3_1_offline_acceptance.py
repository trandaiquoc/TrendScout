"""Run the D6.2.3.1 focused regression suite under strict HTTP transport isolation."""

import importlib
import os
import sys
import unittest
from pathlib import Path

from offline_http_guard import OFFLINE_GUARD_TOTALS, strict_offline_http_guard

MODULES = (
    "test_d5_wikimedia_gateway",
    "test_http_policy",
    "test_d6_wikimedia_synthetic_e2e",
    "test_d6_local_live_http",
    "test_d6_offline_worker_composition",
    "test_distributed_discovery",
    "test_d6_dashboard_capabilities",
    "test_public_fetch",
)


def main():
    tests_dir = Path(__file__).resolve().parent
    backend_dir = tests_dir.parent
    sys.path.insert(0, str(backend_dir))
    sys.path.insert(0, str(tests_dir))
    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    with strict_offline_http_guard() as (counts, _fake_transport):
        for module_name in MODULES:
            suite.addTests(loader.loadTestsFromModule(importlib.import_module(module_name)))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            raise SystemExit(1)
        if counts["real_constructions"] != 0 or counts["requests"] != 0:
            raise AssertionError(f"strict offline transport guard counts: {counts}")
        if (OFFLINE_GUARD_TOTALS["real_constructions"] != 0
                or OFFLINE_GUARD_TOTALS["requests"] != 0):
            raise AssertionError(f"strict offline guard aggregate counts: {OFFLINE_GUARD_TOTALS}")
        print(
            "Strict offline transport guard: ACTIVE; "
            f"real AsyncHTTPTransport constructions={OFFLINE_GUARD_TOTALS['real_constructions']}; "
            f"unmocked transport requests={OFFLINE_GUARD_TOTALS['requests']}; "
            f"fake transport constructions={OFFLINE_GUARD_TOTALS['fake_constructions']}"
        )

        if os.name == "nt":
            from browser_wikimedia_synthetic_e2e_offline import main as browser_e2e

            browser_e2e()
        else:
            print("Native Windows Chrome browser E2E: NOT RUN (non-Windows environment)")


if __name__ == "__main__":
    main()
