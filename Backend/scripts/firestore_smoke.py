"""One-document live smoke; run explicitly with GOOGLE_CLOUD_PROJECT set."""

import asyncio
import os
from uuid import uuid4

from app.storage.firestore import FirestoreStorage


async def main():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project != "trendscout-192e4" or os.environ.get("FIRESTORE_EMULATOR_HOST"):
        raise SystemExit("Smoke requires project trendscout-192e4 and no emulator override")
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
