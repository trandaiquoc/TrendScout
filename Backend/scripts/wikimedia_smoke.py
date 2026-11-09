"""Read-only live smoke; does not write Firestore."""

import asyncio

from app.adapters.wikimedia import WikimediaAdapter


async def main():
    result = await WikimediaAdapter().collect()
    print(f"analysis_date={result.analysis_date} requests={result.request_count}")
    for snapshot, rows in result.days:
        projects = len({row.project for row in rows}) if snapshot.item_count is not None else None
        valid = len(rows) if snapshot.item_count is not None else None
        print(f"{snapshot.observation_date} http={snapshot.http_status} status={snapshot.fetch_status} "
              f"requests={snapshot.request_count} received={snapshot.item_count} valid={valid} "
              f"rejected={snapshot.rejected_count} projects={projects} error={snapshot.error}")
    if result.analysis_date is None:
        raise SystemExit("No analysis date was fetched")


if __name__ == "__main__":
    asyncio.run(main())
