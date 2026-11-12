import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import d6_local_smoke


class SmokeCliTests(unittest.TestCase):
    def test_default_command_never_starts_smoke(self):
        output = io.StringIO()
        with patch.object(d6_local_smoke, "run_smoke") as run, contextlib.redirect_stdout(output):
            self.assertEqual(d6_local_smoke.main([]), 2)
        run.assert_not_called()
        self.assertIn("BLOCKED", output.getvalue())

    def test_request_approval_alone_is_not_quota_or_usage_approval(self):
        with patch.object(d6_local_smoke, "run_smoke") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d6_local_smoke.main(["--owner-approved-four-requests"]), 2)
        run.assert_not_called()


class SmokeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_mock_flow_does_not_store_response_content(self):
        calls = []
        class Credentials:
            def status(self):
                return {"SERPAPI_API_KEY": "PRESENT", "YOUTUBE_API_KEY": "PRESENT"}
        class Gateway:
            def __init__(self, source):
                self.source = source
            async def fetch_page(self, **_kwargs):
                calls.append(self.source)
                return SimpleNamespace(data_mode="SYNTHETIC", page=SimpleNamespace(items=["fixture-private-content"]))
            async def fetch_day(self, **_kwargs):
                calls.append(self.source)
                return SimpleNamespace(data_mode="SYNTHETIC", observations=["fixture-private-content"],
                                       rejected_count=0)
            async def aclose(self):
                pass
        async def query_factory(source, *_args, **_kwargs):
            return Gateway(source)
        async def wiki_factory(*_args, **_kwargs):
            return Gateway(d6_local_smoke.WIKIMEDIA_SOURCE_ID)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(d6_local_smoke, "_LEDGER", Path(directory) / "smoke"), \
                 patch.object(d6_local_smoke, "LocalCredentialProvider", Credentials), \
                 patch.object(d6_local_smoke.LivePageGateway, "controlled_network", query_factory), \
                 patch.object(d6_local_smoke.WikimediaTopPageGateway, "controlled_network", wiki_factory):
                result = await d6_local_smoke.run_smoke()
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(set(calls), set(d6_local_smoke._SOURCES))
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(row["data_mode"] == "SYNTHETIC" for row in result["sources"].values()))
                for path in (Path(directory) / "smoke").iterdir():
                    if path.is_file():
                        self.assertNotIn(b"fixture-private-content", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
