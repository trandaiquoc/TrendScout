"""Windows/POSIX process locking: exclusivity and automatic release after exit."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.storage.local_process_lock import LocalProcessLock
from local_security_helpers import private_test_directory


class LocalProcessLockTests(unittest.TestCase):
    def test_lock_is_exclusive_across_processes_and_crash_releases_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = private_test_directory(temporary)
            path = directory / "guard.lock"
            source = (
                "import sys,time; from app.storage.local_process_lock import LocalProcessLock; "
                "lock=LocalProcessLock(sys.argv[1]); "
                "print('LOCKED' if lock.acquire() else 'BUSY', flush=True); time.sleep(60)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", source, str(path)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "LOCKED")
                self.assertFalse(LocalProcessLock(path).acquire())
                process.terminate()
                process.wait(timeout=10)
                recovered = LocalProcessLock(path)
                self.assertTrue(recovered.acquire())
                recovered.release()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
