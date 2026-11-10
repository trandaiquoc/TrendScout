"""Prove the strict seccomp launcher blocks Python, libc, connect, and children."""

import ctypes
import errno
import socket
import subprocess
import sys


def must_be_blocked(label, operation):
    try:
        operation()
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise SystemExit(f"{label}: expected EPERM, got errno={exc.errno}") from exc
        print(f"{label}: EPERM")
    else:
        raise SystemExit(f"{label}: unexpectedly succeeded")


def main():
    must_be_blocked("python socket AF_INET", lambda: socket.socket(socket.AF_INET))
    must_be_blocked("python socket AF_INET6", lambda: socket.socket(socket.AF_INET6))
    local_socket = socket.socket(socket.AF_UNIX)
    local_socket.close()
    print("python socket AF_UNIX: allowed for local IPC")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    libc.socket.restype = ctypes.c_int

    def native_socket(family):
        fd = libc.socket(family, socket.SOCK_STREAM, 0)
        if fd >= 0:
            import os

            os.close(fd)
            raise OSError(0, "native socket unexpectedly succeeded")
        raise OSError(ctypes.get_errno(), "native socket blocked")

    must_be_blocked("libc socket AF_INET", lambda: native_socket(socket.AF_INET))
    must_be_blocked("libc socket AF_INET6", lambda: native_socket(socket.AF_INET6))

    class SockAddr(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 14)]

    libc.connect.argtypes = [ctypes.c_int, ctypes.POINTER(SockAddr), ctypes.c_uint]
    libc.connect.restype = ctypes.c_int

    def native_connect():
        address = SockAddr(socket.AF_INET, (ctypes.c_ubyte * 14)())
        result = libc.connect(-1, ctypes.byref(address), ctypes.sizeof(address))
        if result == 0:
            raise OSError(0, "native connect unexpectedly succeeded")
        raise OSError(ctypes.get_errno(), "native connect blocked")

    must_be_blocked("libc connect", native_connect)

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.socket(socket.AF_INET); raise SystemExit(9)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if child.returncode == 0 or "Operation not permitted" not in child.stderr:
        raise SystemExit("child process did not inherit the network filter")
    print("child Python socket AF_INET: EPERM")


if __name__ == "__main__":
    main()
