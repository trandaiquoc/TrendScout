"""Explicit, fail-closed Firestore Emulator client for local tests only.

This module never initializes Firebase Admin, ADC, or a production client. The
factory is intentionally separate from the application's normal composition.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import os
import re

from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore


SYNTHETIC_PROJECT_ID = "demo-trendscout-p3s1"
_HOST_RE = re.compile(r"^(127\.0\.0\.1):([0-9]{1,5})$")
_GUARD_TOKEN = object()


class GuardedFirestoreEmulatorClient:
    """Capability wrapper returned only by the validated emulator factory."""

    __slots__ = ("_client", "project_id", "host")

    def __init__(self, client, *, project_id: str, host: str, _token) -> None:
        if _token is not _GUARD_TOKEN:
            raise EmulatorConfigurationError("construct clients through create_guarded_emulator_client")
        self._client = client
        self.project_id = project_id
        self.host = host

    def __getattr__(self, name):
        return getattr(self._client, name)


class EmulatorConfigurationError(RuntimeError):
    """Emulator safety precondition failed before any Firestore operation."""


@dataclass(frozen=True)
class EmulatorTarget:
    host: str
    port: int
    project_id: str


def _target_from_environment(environ: dict[str, str] | None = None) -> EmulatorTarget:
    env = os.environ if environ is None else environ
    host_value = env.get("FIRESTORE_EMULATOR_HOST", "")
    match = _HOST_RE.fullmatch(host_value)
    if match is None:
        raise EmulatorConfigurationError(
            "FIRESTORE_EMULATOR_HOST must be numeric loopback 127.0.0.1:PORT without a scheme"
        )

    port = int(match.group(2))
    if not 1024 <= port <= 65535:
        raise EmulatorConfigurationError("emulator port must be an unprivileged TCP port")

    project_id = env.get("TREND_SCOUT_EMULATOR_PROJECT_ID", "")
    if project_id != SYNTHETIC_PROJECT_ID:
        raise EmulatorConfigurationError("only the dedicated synthetic emulator project is allowed")

    for name in ("GOOGLE_APPLICATION_CREDENTIALS",):
        if env.get(name):
            raise EmulatorConfigurationError("credential configuration is forbidden for emulator tests")

    for name in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        configured = env.get(name)
        if configured and configured != SYNTHETIC_PROJECT_ID:
            raise EmulatorConfigurationError("production project configuration is forbidden")

    return EmulatorTarget("127.0.0.1", port, project_id)


def _assert_network_namespace_isolated() -> None:
    """Require a network namespace exposing only loopback and no external routes."""
    try:
        with open("/proc/net/dev", encoding="ascii") as interface_file:
            interface_lines = interface_file.read().splitlines()[2:]
        interfaces = {
            line.split(":", 1)[0].strip()
            for line in interface_lines
            if ":" in line and line.split(":", 1)[0].strip()
        }
        with open("/proc/net/route", encoding="ascii") as route_file:
            ipv4_routes = [line for line in route_file.read().splitlines()[1:] if line.strip()]
        with open("/proc/net/ipv6_route", encoding="ascii") as route_file:
            ipv6_routes = [
                line for line in route_file.read().splitlines()
                if line.strip() and line.split()[-1] != "lo"
            ]
    except OSError as exc:
        raise EmulatorConfigurationError("cannot verify the isolated network namespace") from exc

    if interfaces != {"lo"} or ipv4_routes or ipv6_routes:
        raise EmulatorConfigurationError(
            "emulator tests must run in a network namespace with only loopback and no external routes"
        )


def _probe_firestore_emulator(target: EmulatorTarget) -> None:
    """Read a synthetic collection through the Firestore Emulator REST surface."""
    connection = http.client.HTTPConnection(target.host, target.port, timeout=1.0)
    path = (
        f"/v1/projects/{target.project_id}/databases/(default)/documents/"
        "ts3_emulator_guard_probe?pageSize=1"
    )
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip()
        body = json.loads(response.read())
        if response.status != 200 or content_type != "application/json":
            raise EmulatorConfigurationError("loopback listener did not identify as Firestore Emulator")
        if not isinstance(body, dict) or not isinstance(body.get("documents"), list):
            raise EmulatorConfigurationError("Firestore Emulator probe response had an unexpected schema")
    except EmulatorConfigurationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EmulatorConfigurationError("Firestore Emulator is unavailable or invalid") from exc
    finally:
        connection.close()


def create_guarded_emulator_client():
    """Return an anonymous SDK client only after strict local safety checks.

    Callers must run inside ``bwrap --unshare-net``. The SDK's emulator mode is
    selected only after the host, synthetic project, isolated network namespace,
    and a read-only emulator REST probe have all passed.
    """
    target = _target_from_environment()
    _assert_network_namespace_isolated()
    _probe_firestore_emulator(target)

    sdk_client = firestore.Client(
        project=target.project_id,
        credentials=AnonymousCredentials(),
        database="(default)",
    )
    return GuardedFirestoreEmulatorClient(
        sdk_client,
        project_id=target.project_id,
        host=f"{target.host}:{target.port}",
        _token=_GUARD_TOKEN,
    )
