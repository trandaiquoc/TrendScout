"""Filesystem helpers for tests that exercise actual local ACLs on Windows."""

import os
import subprocess
from pathlib import Path

from app.storage.local_file_security import secure_private_directory


def private_test_directory(parent: str | os.PathLike[str], name="private") -> Path:
    path = Path(parent) / name
    secure_private_directory(path)
    return path


def grant_authenticated_users(path: str | os.PathLike[str], rights="R") -> None:
    result = subprocess.run(
        ["icacls.exe", str(path), "/grant", f"*S-1-5-11:{rights}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError("could not create the intentionally unsafe Windows ACL fixture")
