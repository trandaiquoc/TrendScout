"""Live, read-only Wikimedia -> cleaning -> keywords -> scoring report."""

import asyncio
from collections import Counter

from app.adapters.wikimedia import WikimediaAdapter
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends
from app.processing.wikimedia_cleaning import clean_snapshot


def describe(row):
    return (f"{row.canonical_keyword} | project={row.project} | score={row.score} | status={row.status} | "
            f"current={row.current_views_ceil} | baseline={row.baseline} | ratio={row.ratio} | "
            f"observed={row.observed_baseline_days} | missing={[(d.isoformat(), reason) for d, reason in row.missing_baseline_dates]} | url={row.evidence_url}")


async def main():
    collected = await WikimediaAdapter().collect()
    if collected.analysis_date is None:
        raise SystemExit("No analysis date available")
    days = []
    observations = 0
    for snapshot, rows in collected.days:
        clean = clean_snapshot(snapshot, rows)
        keywords = extract_keywords(clean.accepted)
        observations += len(keywords)
        days.append(DayData(snapshot, tuple(keywords), tuple(rows)))
        print(f"DAY {snapshot.observation_date} http={snapshot.http_status} status={snapshot.fetch_status} raw={clean.report.raw_count} accepted={clean.report.accepted_count}")
    scores = score_trends(collected.analysis_date, days)
    statuses = Counter(row.status for row in scores)
    scored = [row for row in scores if row.score is not None]
    top_score = sorted(scored, key=lambda x: (-x.score, -x.ratio, -x.current_views_ceil, x.project, x.article_original))
    top_volume = sorted(scores, key=lambda x: (-(x.current_views_ceil if x.current_views_ceil is not None else -1), x.project, x.article_original))
    volume_scored = sorted(scored, key=lambda x: (-x.current_views_ceil, x.project, x.article_original))
    overlap = len({x.keyword_id for x in top_score[:10]} & {x.keyword_id for x in volume_scored[:10]})
    print(f"SUMMARY analysis_date={collected.analysis_date} requests={collected.request_count} observations={observations} current_entities={len(scores)} scored={len(scored)} insufficient={len(scores)-len(scored)} statuses={dict(statuses)} scored_top10_overlap_volume_scored_top10={overlap}")
    for header, subset in (("TOP_SCORE", top_score[:10]), ("TOP_VOLUME_ALL", top_volume[:10]),
                           ("HIGH_VOLUME_LOW_GROWTH", [x for x in volume_scored if x.ratio <= 1.1][:3]),
                           ("INSUFFICIENT", [x for x in top_volume if x.score is None][:3])):
        print(header)
        for row in subset:
            print(describe(row))


if __name__ == "__main__":
    asyncio.run(main())
