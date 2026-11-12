"""Platform-specific checks for local files that may hold credentials or quota state.

POSIX checks use ownership and mode bits. Windows checks use the file owner and
the DACL from the native Windows security descriptor; only the owner, SYSTEM,
and local Administrators may have allow ACEs. Administrators are explicitly
trusted because they can take ownership of local files regardless of DACL.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


class UnsafeLocalFile(ValueError):
    """The local path does not meet the platform's private-file requirements."""


def _windows_security():
    if os.name != "nt":
        raise RuntimeError("Windows security is unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.CopySid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi.CopySid.restype = wintypes.BOOL
    advapi.CreateWellKnownSid.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    advapi.CreateWellKnownSid.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return ctypes, wintypes, advapi, kernel


def _windows_current_sid():
    ctypes, wintypes, advapi, kernel = _windows_security()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise UnsafeLocalFile("cannot inspect current Windows user")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise UnsafeLocalFile("cannot inspect current Windows user")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        length = advapi.GetLengthSid(sid)
        copied = ctypes.create_string_buffer(length)
        if not advapi.CopySid(length, copied, sid):
            raise UnsafeLocalFile("cannot copy current Windows user SID")
        return copied
    finally:
        kernel.CloseHandle(token)


def _windows_well_known_sid(kind: int):
    ctypes, wintypes, advapi, _kernel = _windows_security()
    buffer = ctypes.create_string_buffer(68)
    size = wintypes.DWORD(len(buffer))
    if not advapi.CreateWellKnownSid(kind, None, buffer, ctypes.byref(size)):
        raise UnsafeLocalFile("cannot inspect Windows system principals")
    return buffer


def _windows_verify_acl(path: Path, *, directory: bool) -> None:
    ctypes, wintypes, advapi, kernel = _windows_security()
    _windows_verify_path_type(path, directory=directory, kernel=kernel)
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(
        str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if result != 0 or not owner.value or not dacl.value:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        raise UnsafeLocalFile("Windows owner or discretionary ACL is unavailable")
    try:
        current = _windows_current_sid()
        system = _windows_well_known_sid(22)  # WinLocalSystemSid
        admins = _windows_well_known_sid(26)  # WinBuiltinAdministratorsSid
        owner_rights = ctypes.create_string_buffer(
            bytes((1, 1, 0, 0, 0, 0, 0, 3, 4, 0, 0, 0))
        )  # S-1-3-4 applies only to the already-verified owner.
        if not advapi.EqualSid(owner, current):
            raise UnsafeLocalFile("local path is not owned by the current Windows user")

        # ACL header AceCount follows the 8-byte ACL header.
        ace_count = ctypes.c_ushort.from_address(dacl.value + 4).value
        for index in range(ace_count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise UnsafeLocalFile("Windows ACL cannot be inspected")
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            if ace_type == 0:  # ACCESS_ALLOWED_ACE_TYPE
                sid_address = ace.value + 8
                sid = ctypes.c_void_p(sid_address)
                if not (advapi.EqualSid(sid, current)
                        or advapi.EqualSid(sid, system)
                        or advapi.EqualSid(sid, admins)
                        or advapi.EqualSid(sid, owner_rights)):
                    raise UnsafeLocalFile(
                        f"Windows ACL grants another principal access (ACE {index})"
                    )
            elif ace_type in (5, 9, 11):
                raise UnsafeLocalFile("Windows ACL contains an unsupported allow ACE")
            elif ace_type not in (1, 6, 10, 12, 2, 7, 8, 13):
                raise UnsafeLocalFile("Windows ACL contains an unsupported ACE type")
    finally:
        kernel.LocalFree(descriptor)


def _windows_verify_path_type(path: Path, *, directory: bool, kernel=None) -> None:
    if kernel is None:
        _ctypes, _wintypes, _advapi, kernel = _windows_security()
    attributes = kernel.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF or attributes & 0x400:
        raise UnsafeLocalFile("local path is unavailable or is a reparse point")
    if bool(attributes & 0x10) != directory:
        raise UnsafeLocalFile("local path has an unexpected file type")


def verify_private_directory(path: str | os.PathLike[str]) -> None:
    """Verify that a directory is owned and accessible only by trusted principals."""
    target = Path(path)
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise UnsafeLocalFile("local storage directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or target.is_symlink():
        raise UnsafeLocalFile("local storage path must be a real directory")
    if os.name == "nt":
        _windows_verify_acl(target, directory=True)
    elif metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise UnsafeLocalFile("local storage directory must be owner-only")


def verify_private_file(path: str | os.PathLike[str]) -> None:
    """Verify a regular file is owned and accessible only by trusted principals."""
    target = Path(path)
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise UnsafeLocalFile("local file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or target.is_symlink():
        raise UnsafeLocalFile("local path must be a regular file")
    if os.name == "nt":
        _windows_verify_acl(target, directory=False)
    elif metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise UnsafeLocalFile("local file must be owner-only")


def secure_private_directory(path: str | os.PathLike[str]) -> None:
    """Create a directory and remove inherited Windows grants outside trusted SIDs."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise UnsafeLocalFile("local storage directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or target.is_symlink():
        raise UnsafeLocalFile("local storage path must be a real directory")
    if os.name == "nt":
        _windows_verify_path_type(target, directory=True)
        current = _windows_current_sid()
        ctypes, _wintypes, advapi, _kernel = _windows_security()
        string_sid = ctypes.c_void_p()
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi.ConvertSidToStringSidW.restype = ctypes.c_int
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
        if not advapi.ConvertSidToStringSidW(current, ctypes.byref(string_sid)):
            raise UnsafeLocalFile("cannot identify current Windows user")
        try:
            owner_sid = ctypes.wstring_at(string_sid)
        finally:
            ctypes.windll.kernel32.LocalFree(string_sid)
        # icacls is the supported Windows ACL management utility. No shell is
        # used; inherited grants are removed and trusted principals are granted.
        result = subprocess.run(
            ["icacls.exe", str(target), "/inheritance:r", "/grant:r",
             f"*{owner_sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
             "*S-1-5-32-544:(OI)(CI)F"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise UnsafeLocalFile("cannot establish a private Windows storage ACL")
    else:
        os.chmod(target, 0o700)
    verify_private_directory(target)
