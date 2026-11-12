import os
import tempfile
import unittest
from pathlib import Path

from app.local_credentials import InvalidCredentialConfig, LocalCredentialProvider


class LocalCredentialProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "demo.env"
        self.provider = LocalCredentialProvider(self.path)

    def write(self, value, mode=0o600):
        self.path.write_text(value, encoding="utf-8")
        self.path.chmod(mode)

    def test_missing_and_present_without_exposing_values_in_status(self):
        self.assertEqual(set(self.provider.status().values()), {"MISSING"})
        self.write("OTHER_SETTING=unrelated\nYOUTUBE_API_KEY=sample-key-123\nSERPAPI_API_KEY=\n")
        self.assertEqual(self.provider.resolve("YOUTUBE_API_KEY"), "sample-key-123")
        self.assertIsNone(self.provider.resolve("OTHER"))
        self.assertEqual(self.provider.status(), {
            "SERPAPI_API_KEY": "MISSING", "YOUTUBE_API_KEY": "PRESENT"})

    def test_rejects_shared_permissions_symlink_and_malformed_input(self):
        for contents, mode in (("YOUTUBE_API_KEY=sample-key-123\n", 0o644),
                               ("YOUTUBE_API_KEY=abc def ghi\n", 0o600),
                               ("YOUTUBE_API_KEY=sample-key-123\nYOUTUBE_API_KEY=other-key-123\n", 0o600),
                               ("export YOUTUBE_API_KEY=sample-key-123\n", 0o600)):
            with self.subTest(mode=mode, length=len(contents)):
                self.write(contents, mode)
                self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})
                with self.assertRaises(InvalidCredentialConfig):
                    self.provider.resolve("YOUTUBE_API_KEY")
        self.path.unlink()
        self.path.symlink_to(Path(self.directory.name) / "target")
        self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})

    def test_non_regular_file_is_rejected_without_blocking(self):
        os.mkfifo(self.path, 0o600)
        self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})


if __name__ == "__main__":
    unittest.main()
