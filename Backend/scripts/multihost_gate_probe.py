"""Synthetic Firestore-backed two-host worker probe; never contacts providers.

This opt-in harness writes bounded coordination state into the existing
``_ts_d5_coordination`` collection. Run only against an approved, non-production
project. It is a verification tool, not the public Fetch runtime composition.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import signal
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import google.auth
from google.cloud import firestore
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as KeyFileCredentials

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.multihost_probe import register_host
from app.jobs.public_admission import AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.jobs.worker_runner import DistributedWorkerRunner
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore

_RUN_ID = re.compile(r"^[a-f0-9]{8,32}$")
_HOST_ID = re.compile(r"^[A-Za-z0-9._~-]{1,40}$")


def _now():
    return datetime.now(timezone.utc)


def _machine_fingerprint():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value:
            return hashlib.sha256(value.encode("ascii")).hexdigest()
    raise RuntimeError("Linux machine-id unavailable; cannot distinguish worker hosts")


def _store(project_id):
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/datastore"]
    )
    if isinstance(credentials, (UserCredentials, KeyFileCredentials)):
        raise RuntimeError("user OAuth and key-file credentials are forbidden; use an attached organization runtime identity")
    client = firestore.Client(project=project_id, credentials=credentials)
    return FirestoreAtomicStateStore(client)


def _wait_for_peers(store, *, run_id, host_id, machine_fingerprint,
                    expected_hosts, timeout_seconds=120):
    if (len(expected_hosts) != 2 or len(set(expected_hosts)) != 2
            or host_id not in expected_hosts):
        raise ValueError("exactly two distinct expected host IDs are required")
    deadline = time.monotonic() + timeout_seconds
    delay = 0.25
    while time.monotonic() < deadline:
        count = register_host(store, run_id=run_id, host_id=host_id,
                              machine_fingerprint=machine_fingerprint,
                              expected_hosts=expected_hosts)
        if count == 2:
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 3.0)
    raise TimeoutError("second independent host did not register before deadline")


def _policy():
    return AdmissionPolicy(
        enabled_sources=frozenset({WIKIMEDIA_SOURCE_ID}),
        pools={"wikimedia": PoolLimit(5, 10, 5, 10, 1, max_inflight=1)},
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        max_starts_per_window=5, window_seconds=3600, cooldown_seconds=0,
        lease_seconds=30, max_attempts=1, max_job_seconds=120,
        receipt_seconds=300, max_records=50, max_status_polls=20, enabled=True,
    )


def _admission(store, run_id):
    return PublicAdmission(store, _policy(), state_key=f"d5mh-fetch-{run_id}")


def _grant(source_id, now):
    return CheckpointGrant(source_id, "SYNTHETIC", "d5-multihost-probe-v1",
                           now + timedelta(minutes=5), 300, True)


def _worker(store, run_id, host_id, machine_fingerprint):
    admission = _admission(store, run_id)

    async def synthetic_handler(_context):
        return {
            "status": "SUCCESS", "data_mode": "SYNTHETIC",
            "records": [{"fixture": "D5_MULTIHOST_PROBE", "worker_host_id": host_id,
                         "machine_fingerprint": machine_fingerprint}],
            "record_count": 1,
        }

    return admission, DistributedFetchWorker(
        admission, handlers={WIKIMEDIA_SOURCE_ID: synthetic_handler},
        resolve_grant=_grant,
    )


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True,
                        help="Existing organization-owned non-production Google Cloud project")
    parser.add_argument("--confirm-nonproduction-project", required=True,
                        help="Repeat the project ID after verifying it is non-production and organization-owned")
    parser.add_argument("--run-id", required=True, type=lambda v: _validated(v, _RUN_ID, "run ID"))
    parser.add_argument("--acknowledge-nonproduction-writes", action="store_true",
                        help="Required: this probe writes test state to Firestore")
    commands = parser.add_subparsers(dest="action", required=True)
    worker = commands.add_parser("worker", help="Register this host and run a supervised synthetic worker")
    worker.add_argument("--host-id", required=True, type=lambda v: _validated(v, _HOST_ID, "host ID"))
    worker.add_argument("--expected-hosts", required=True,
                        help="Two comma-separated IDs, identical on both hosts")
    worker.add_argument("--idle-poll-seconds", type=float, default=0.5)
    submit = commands.add_parser("submit", help="Queue one bounded synthetic test job")
    submit.add_argument("--idempotency-key", required=True)
    inspect = commands.add_parser("inspect", help="Read one job's state and synthetic result")
    inspect.add_argument("--job-id", required=True)
    return parser


def _validated(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid {label}")
    return value


async def _run_worker(args, store):
    host_id = args.host_id
    expected = tuple(sorted(value.strip() for value in args.expected_hosts.split(",")))
    fingerprint = _machine_fingerprint()
    _wait_for_peers(store, run_id=args.run_id, host_id=host_id,
                    machine_fingerprint=fingerprint, expected_hosts=expected)
    admission, worker = _worker(store, args.run_id, host_id, fingerprint)
    runner = DistributedWorkerRunner(worker, owner=host_id,
                                     idle_poll_seconds=args.idle_poll_seconds)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    print(json.dumps({"event": "worker_ready", "host_id": host_id,
                      "hostname": socket.gethostname(), "run_id": args.run_id}), flush=True)
    completed = await runner.run(stop)
    print(json.dumps({"event": "worker_stopped", "host_id": host_id,
                      "completed_jobs": completed}), flush=True)


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.confirm_nonproduction_project != args.project_id:
        raise SystemExit("project confirmation does not match --project-id")
    if not args.acknowledge_nonproduction_writes:
        raise SystemExit("refusing Firestore writes without --acknowledge-nonproduction-writes")
    store = _store(args.project_id)
    if args.action == "worker":
        asyncio.run(_run_worker(args, store))
        return 0
    admission = _admission(store, args.run_id)
    if args.action == "submit":
        print(json.dumps(admission.admit(args.idempotency_key, now=_now()), sort_keys=True))
        return 0
    job = admission.read_job(args.job_id, now=_now())
    if job is None:
        print(json.dumps({"job_id": args.job_id, "status": "NOT_FOUND"}, sort_keys=True))
        return 1
    source = job.get("source_checkpoints", {}).get(WIKIMEDIA_SOURCE_ID, {}).get("snapshot", {})
    result = source.get("result", {}) if source.get("phase") == "COMPLETE" else {}
    print(json.dumps({"job_id": args.job_id, "status": job.get("state"),
                      "synthetic_result": result}, sort_keys=True))
    return 0 if job.get("state") in {"COMPLETED", "PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
