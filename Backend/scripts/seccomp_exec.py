"""Execute a command with Linux seccomp socket families constrained.

Strict mode denies Internet socket creation, all connect calls, and io_uring
for this process tree while allowing AF_UNIX socketpairs used by asyncio. The
optional Unix-only mode allows AF_UNIX for local Chrome IPC while blocking IP
sockets. The emulator mode allows only AF_INET/AF_INET6; it must be combined
with an isolated network namespace that has loopback and no external routes.
This is a test launcher, not a production firewall.
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
AF_INET = 2
AF_INET6 = 10
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_RET_K = 0x06
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_KILL_PROCESS = 0x80000000


class ArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


class SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def _load_ip_socket_filter(libc, seccomp) -> None:
    """Allow only Internet socket families; network namespace does egress deny."""
    numbers = {
        name: seccomp.seccomp_syscall_resolve_name(name.encode())
        for name in ("socket", "socketpair", "io_uring_setup")
    }
    if any(number < 0 for number in numbers.values()):
        raise SystemExit("required syscall names could not be resolved")

    seccomp.seccomp_arch_native.restype = ctypes.c_uint32
    native_arch = seccomp.seccomp_arch_native()

    instructions = (SockFilter * 13)(
        SockFilter(BPF_LD_W_ABS, 0, 0, 4),
        SockFilter(BPF_JMP_JEQ_K, 1, 0, native_arch),
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        SockFilter(BPF_LD_W_ABS, 0, 0, 0),
        SockFilter(BPF_JMP_JEQ_K, 3, 0, numbers["socket"]),
        SockFilter(BPF_JMP_JEQ_K, 5, 0, numbers["socketpair"]),
        SockFilter(BPF_JMP_JEQ_K, 4, 0, numbers["io_uring_setup"]),
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        SockFilter(BPF_LD_W_ABS, 0, 0, 16),
        SockFilter(BPF_JMP_JEQ_K, 2, 0, AF_INET),
        SockFilter(BPF_JMP_JEQ_K, 1, 0, AF_INET6),
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM),
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
    )
    program = SockFprog(len(instructions), instructions)

    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(
        PR_SET_SECCOMP,
        SECCOMP_MODE_FILTER,
        ctypes.cast(ctypes.pointer(program), ctypes.c_void_p),
        0,
        0,
    ) != 0:
        raise OSError(ctypes.get_errno(), "IP-only seccomp filter setup failed")


def _load_filter(allow_unix_sockets: bool, allow_ip_sockets: bool) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
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

    if allow_ip_sockets:
        _load_ip_socket_filter(libc, seccomp)
        return

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

    if not allow_unix_sockets and not allow_ip_sockets:
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--allow-unix-sockets",
        action="store_true",
        help="allow AF_UNIX only for local browser IPC; deny all IP socket families",
    )
    mode.add_argument(
        "--allow-ip-sockets",
        action="store_true",
        help="allow AF_INET/AF_INET6; use only inside a loopback-only network namespace",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    command = options.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("provide a command after --")

    _load_filter(options.allow_unix_sockets, options.allow_ip_sockets)
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    os.closerange(3, soft)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
