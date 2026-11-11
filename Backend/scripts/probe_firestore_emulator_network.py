"""Verify emulator runner socket-family and namespace isolation together."""

import errno
import socket
import subprocess
import sys
import threading


def expect_epperm(label, operation):
    try:
        operation()
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise SystemExit(f"{label}: expected EPERM, got errno={exc.errno}") from exc
    else:
        raise SystemExit(f"{label}: unexpectedly succeeded")


def main():
    expect_epperm("AF_UNIX socket", lambda: socket.socket(socket.AF_UNIX))
    expect_epperm("socketpair", lambda: socket.socketpair())

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted = threading.Event()

    def accept_once():
        connection, _ = server.accept()
        connection.close()
        accepted.set()

    worker = threading.Thread(target=accept_once, daemon=True)
    worker.start()
    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
        pass
    if not accepted.wait(1.0):
        raise SystemExit("loopback server did not accept connection")
    server.close()

    public = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    public.settimeout(1.0)
    result = public.connect_ex(("1.1.1.1", 443))
    public.close()
    if result == 0:
        raise SystemExit("public IPv4 connection unexpectedly succeeded")
    if result not in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPERM):
        raise SystemExit(f"public IPv4 connection had unexpected result: {result}")

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.socket(socket.AF_UNIX); raise SystemExit(9)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if child.returncode == 0 or "Operation not permitted" not in child.stderr:
        raise SystemExit("child process did not inherit the AF_UNIX seccomp block")

    print("emulator network sandbox: loopback PASS; public route BLOCKED; AF_UNIX EPERM")


if __name__ == "__main__":
    main()
