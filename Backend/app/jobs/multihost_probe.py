"""Host identity registration for the opt-in multi-host infrastructure probe."""

import re
import socket
from datetime import datetime, timezone


def register_host(store, *, run_id, host_id, machine_fingerprint, expected_hosts):
    """Atomically register a host and reject duplicate machine identities."""
    if (not callable(getattr(store, "transact", None))
            or not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{8,32}", run_id)
            or not isinstance(host_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,40}", host_id)
            or not isinstance(machine_fingerprint, str)
            or not re.fullmatch(r"[a-f0-9]{64}", machine_fingerprint)
            or not isinstance(expected_hosts, tuple) or len(expected_hosts) != 2
            or len(set(expected_hosts)) != 2 or host_id not in expected_hosts):
        raise ValueError("invalid multi-host registration request")
    key = f"d5mh-host-{run_id}"
    now = datetime.now(timezone.utc).isoformat()

    def operation(state):
        state = state or {"schema": 1, "run_id": run_id,
                          "expected_hosts": list(expected_hosts), "hosts": {}}
        if (state.get("schema") != 1 or state.get("run_id") != run_id
                or state.get("expected_hosts") != list(expected_hosts)
                or not isinstance(state.get("hosts"), dict)):
            raise ValueError("multi-host run scope mismatch")
        existing = state["hosts"].get(host_id)
        if (isinstance(existing, dict)
                and existing.get("machine_fingerprint") != machine_fingerprint):
            raise ValueError("host ID changed machine identity")
        for other_id, entry in state["hosts"].items():
            if (other_id != host_id and isinstance(entry, dict)
                    and entry.get("machine_fingerprint") == machine_fingerprint):
                raise ValueError("host IDs resolve to the same machine identity")
        state["hosts"][host_id] = {"machine_fingerprint": machine_fingerprint,
                                   "hostname": socket.gethostname(), "seen_at": now}
        return state, len(state["hosts"])

    return store.transact(key, operation)
