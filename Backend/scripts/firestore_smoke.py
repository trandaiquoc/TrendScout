"""Bounded Firestore smoke for an explicitly approved non-production project."""

import argparse
import asyncio
import os
from uuid import uuid4

from app.storage.firestore import FirestoreStorage

_PRODUCTION_PROJECT_IDENTIFIERS = frozenset({"trendscout-192e4", "556422620769"})


def _validate_target(
    project: str | None,
    confirmed_project: str,
    *,
    acknowledge_nonproduction_writes: bool,
    emulator_host: str | None,
) -> None:
    if not project or not project.strip() or project != project.strip():
        raise SystemExit(
            "GOOGLE_CLOUD_PROJECT must be a non-empty project ID without surrounding whitespace"
        )
    if project.casefold() in _PRODUCTION_PROJECT_IDENTIFIERS:
        raise SystemExit("refusing smoke writes to the TrendScout production project")
    if project != confirmed_project:
        raise SystemExit("confirmed non-production project does not match GOOGLE_CLOUD_PROJECT")
    if emulator_host:
        raise SystemExit("live smoke does not accept a Firestore Emulator override")
    if not acknowledge_nonproduction_writes:
        raise SystemExit("refusing Firestore writes without --acknowledge-nonproduction-writes")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-nonproduction-project", required=True)
    parser.add_argument("--acknowledge-nonproduction-writes", action="store_true")
    args = parser.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    _validate_target(
        project,
        args.confirm_nonproduction_project,
        acknowledge_nonproduction_writes=args.acknowledge_nonproduction_writes,
        emulator_host=os.environ.get("FIRESTORE_EMULATOR_HOST"),
    )
    storage = FirestoreStorage.from_project(project)
    if storage._client.project != project or storage._client._database != "(default)":
        raise SystemExit("Unexpected project or database; aborting")
    run_id = f"step3-smoke-{uuid4().hex}"
    print(f"Smoke document: analysis_runs/{run_id}", flush=True)
    doc = storage._client.collection("analysis_runs").document(run_id)
    created = False
    try:
        await storage.create_run(run_id, {"purpose": "step3-smoke", "status": "TEST", "zero": 0, "null": None})
        created = True
        data = await storage.get_run(run_id)
        if data != {"purpose": "step3-smoke", "status": "TEST", "zero": 0, "null": None}:
            raise RuntimeError("Read-back mismatch")
        await storage.update_run(run_id, {"status": "UPDATED"})
        if (await storage.get_run(run_id))["status"] != "UPDATED":
            raise RuntimeError("Update read-back mismatch")
        print(f"CREATE/READ/UPDATE OK: analysis_runs/{run_id}", flush=True)
    finally:
        if created:
            await asyncio.to_thread(doc.delete)
            if (await asyncio.to_thread(doc.get)).exists:
                raise RuntimeError(f"Cleanup failed: analysis_runs/{run_id}")
            print(f"DELETE verified: analysis_runs/{run_id}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
