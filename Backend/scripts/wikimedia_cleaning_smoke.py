"""Live read-only collection and cleaning report; no Firestore writes."""

import asyncio
from app.adapters.wikimedia import WikimediaAdapter
from app.processing.wikimedia_cleaning import clean_snapshot


async def main():
    collected = await WikimediaAdapter().collect()
    print(f"analysis_date={collected.analysis_date} requests={collected.request_count}")
    if collected.analysis_date is None:
        raise SystemExit("No analysis date available")
    for snapshot, rows in collected.days:
        result = clean_snapshot(snapshot, rows)
        r = result.report
        print(f"{r.observation_date} http={snapshot.http_status} status={r.fetch_status} requests={snapshot.request_count} "
              f"raw={r.raw_count} accepted={r.accepted_count} rejected={r.rejected_count} duplicates={r.duplicate_count} reasons={r.rejection_reasons}")
        if snapshot.observation_date == collected.analysis_date:
            print("ACCEPTED EXAMPLES:", [(x.project, x.article_original, x.canonical_keyword) for x in result.accepted[:5]])
            print("REJECTED EXAMPLES:", [(x.observation.project, x.observation.article_original, x.reason) for x in result.rejected[:8]])


if __name__ == "__main__":
    asyncio.run(main())
