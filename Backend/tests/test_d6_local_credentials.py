import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.local_credentials import InvalidCredentialConfig, LocalCredentialProvider
from app.storage.local_file_security import verify_private_directory
from local_security_helpers import grant_authenticated_users, private_test_directory


class LocalCredentialProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = private_test_directory(self.directory.name)
        self.path = self.parent / "demo.env"
        self.provider = LocalCredentialProvider(self.path)

    def write(self, value, mode=0o600):
        self.path.unlink(missing_ok=True)
        self.path.write_text(value, encoding="utf-8")
        if os.name == "nt":
            if mode & 0o077:
                grant_authenticated_users(self.path, "R")
        else:
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
        if os.name == "nt":
            self.path.write_text("YOUTUBE_API_KEY=sample-key-123\n", encoding="utf-8")
            junction = Path(self.directory.name) / "junction"
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J",
                 str(junction), str(self.parent)],
                check=True,
                capture_output=True,
            )
            linked_provider = LocalCredentialProvider(junction / self.path.name)
            self.assertEqual(set(linked_provider.status().values()), {"INVALID_CONFIG"})
        else:
            target = self.parent / "target"
            target.write_text("YOUTUBE_API_KEY=sample-key-123\n", encoding="utf-8")
            self.path.symlink_to(target)
            self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})

    def test_non_regular_file_is_rejected_without_blocking(self):
        if os.name == "nt":
            self.path.mkdir()
        else:
            os.mkfifo(self.path, 0o600)
        self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})

    @unittest.skipUnless(os.name == "nt", "Windows ACL behavior")
    def test_windows_acl_rejects_ordinary_user_read_access(self):
        self.write("YOUTUBE_API_KEY=fixture-key-123\n")
        grant_authenticated_users(self.path, "R")
        self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})

    @unittest.skipUnless(os.name == "nt", "Windows ACL behavior")
    def test_windows_parent_acl_must_not_grant_other_users_access(self):
        self.write("YOUTUBE_API_KEY=fixture-key-123\n")
        grant_authenticated_users(self.parent, "(OI)(CI)M")
        with self.assertRaises(ValueError):
            verify_private_directory(self.parent)
        self.assertEqual(set(self.provider.status().values()), {"INVALID_CONFIG"})


if __name__ == "__main__":
    unittest.main()
