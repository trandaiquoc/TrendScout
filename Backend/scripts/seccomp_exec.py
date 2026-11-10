"""Execute a command with Linux seccomp network syscalls denied.

Strict mode denies Internet socket creation, all connect calls, and io_uring
for this process tree while allowing AF_UNIX socketpairs used by asyncio. The
optional Unix-only mode also allows AF_UNIX connect for local Chrome IPC;
AF_INET/AF_INET6 and other non-AF_UNIX socket creation remain denied. This is a
test launcher, not a production firewall.
"""

import argparse
import ctypes
import errno
import os
import resource
import sys


ALLOW = 0x7FFF0000
ERRNO = 0x00050000
SCMP_CMP_NE = 1
AF_UNIX = 1
PR_SET_NO_NEW_PRIVS = 38


class ArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def _load_filter(allow_unix_sockets: bool) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")

    try:
        seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise SystemExit(f"Linux libseccomp is required: {exc}") from exc

    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgCmp),
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]

    context = seccomp.seccomp_init(ALLOW)
    if not context:
        raise SystemExit("seccomp_init failed")

    denied = ERRNO | errno.EPERM
    for name in (b"socket", b"socketpair"):
        syscall = seccomp.seccomp_syscall_resolve_name(name)
        if syscall < 0:
            continue
        comparison = ArgCmp(0, SCMP_CMP_NE, AF_UNIX, 0)
        result = seccomp.seccomp_rule_add_array(
            context, denied, syscall, 1, ctypes.byref(comparison)
        )
        if result != 0:
            raise SystemExit(f"seccomp rule setup failed for {name.decode()}")

    if not allow_unix_sockets:
        syscall = seccomp.seccomp_syscall_resolve_name(b"connect")
        if syscall >= 0 and seccomp.seccomp_rule_add(context, denied, syscall, 0) != 0:
            raise SystemExit("seccomp rule setup failed for connect")

    syscall = seccomp.seccomp_syscall_resolve_name(b"io_uring_setup")
    if syscall >= 0 and seccomp.seccomp_rule_add(context, denied, syscall, 0) != 0:
        raise SystemExit("seccomp rule setup failed for io_uring_setup")

    if seccomp.seccomp_load(context) != 0:
        raise OSError(ctypes.get_errno(), "seccomp_load failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-unix-sockets",
        action="store_true",
        help="allow AF_UNIX only for local browser IPC; still deny Internet socket families",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    command = options.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("provide a command after --")

    _load_filter(options.allow_unix_sockets)
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    os.closerange(3, soft)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
