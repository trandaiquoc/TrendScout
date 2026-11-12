"""Native Chrome discovery and local DevTools transport for offline browser tests."""

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path


def require_linux_network_isolation():
    """The Linux runner supplies seccomp; Windows uses file pages and fake fetches."""
    if os.name == "nt":
        return
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            probe = socket.socket(family)
            probe.close()
        except PermissionError:
            continue
        raise SystemExit("Internet blocker is not active")


def find_chrome():
    candidates = [os.environ.get("CHROME_BIN")]
    candidates.extend(shutil.which(name) for name in ("chrome", "chrome.exe", "google-chrome", "chromium"))
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return next((item for item in candidates if item and Path(item).is_file()), None)


class ChromeDevTools:
    """Small masked WebSocket client for Chrome's loopback CDP endpoint."""

    def __init__(self, chrome, profile):
        self.profile = str(profile)
        self.active_port = Path(self.profile) / "DevToolsActivePort"
        Path(self.profile).mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen([
            chrome, "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-background-networking", "--disable-extensions", "--no-proxy-server",
            "--disable-sync", "--disable-component-update", "--allow-file-access-from-files",
            "--no-first-run",
            "--remote-debugging-port=0", "--user-data-dir=" + self.profile, "about:blank",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Chrome exited before opening its local DevTools port")
            try:
                port, _browser = self.active_port.read_text(encoding="utf-8").splitlines()[:2]
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        else:
            self.close()
            raise RuntimeError("Chrome DevTools port did not become ready")
        self.socket = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
        self.pending = b""
        self.counter = 0
        self.events = []
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        # Chrome requires the browser UUID in the URL. Discover it from the JSON endpoint.
        self.socket.close()
        import urllib.request

        port = self.active_port.read_text(encoding="utf-8").splitlines()[0]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
            browser_ws = json.load(response)["webSocketDebuggerUrl"]
        path = "/" + browser_ws.split("/", 3)[3]
        self.socket = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.socket.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("Chrome DevTools WebSocket handshake closed")
            response.extend(chunk)
        headers, self.pending = bytes(response).split(b"\r\n\r\n", 1)
        if not headers.startswith(b"HTTP/1.1 101 ") or expected.encode("ascii") not in headers:
            raise RuntimeError(f"Chrome DevTools WebSocket handshake was rejected: {headers!r}")

    def _send(self, payload, opcode=1):
        mask = secrets.token_bytes(4)
        size = len(payload)
        prefix = bytes((0x80 | opcode, 0x80 | min(size, 125)))
        if size >= 126 and size <= 65535:
            prefix = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size)
        elif size > 65535:
            prefix = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", size)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(prefix + mask + masked)

    def _receive(self):
        chunks = []
        while True:
            header = self._recv_exact(2)
            first, second = header
            opcode, final = first & 0x0F, bool(first & 0x80)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._recv_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if second & 0x80 else b""
            data = self._recv_exact(size)
            if mask:
                data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
            if opcode == 8:
                raise RuntimeError("Chrome DevTools WebSocket closed")
            if opcode == 9:
                self._send(data, opcode=10)
                continue
            if opcode in (1, 0):
                chunks.append(data)
                if final:
                    return json.loads(b"".join(chunks))

    def _recv_exact(self, size):
        while len(self.pending) < size:
            chunk = self.socket.recv(max(4096, size - len(self.pending)))
            if not chunk:
                raise RuntimeError("Chrome DevTools WebSocket closed")
            self.pending += chunk
        result, self.pending = self.pending[:size], self.pending[size:]
        return result

    def call(self, method, params=None, session=None):
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._send(json.dumps(message).encode("utf-8"))
        while True:
            reply = self._receive()
            if reply.get("id") != self.counter:
                self.events.append(reply)
                continue
            if "error" in reply:
                raise RuntimeError(str(reply["error"]))
            return reply.get("result", {})

    def close(self):
        connection = getattr(self, "socket", None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
