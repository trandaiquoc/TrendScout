# DB06 Fetch Performance Report

Date: 2026-11-27

Branch: `integration/demo-final`

Implementation baseline: `88e0ed452bb23a3921a2a7b0a0bf6a35e7fa04b0`

## Outcome

DB06E unchanged-input reuse is verified: 1,500-record warm unchanged Fetch median was 3.199 s, payload bytes read were 0, the durable dataset ID and all records/lineage were preserved, and AI remained independently `MODEL_UNAVAILABLE`. DB06F selective persistence works, but changed-input Fetch remains **BLOCKED at P5**: the 1,500-record 1%, 10%, and 100% cases took 18.453 s, 20.925 s, and 28.152 s, all above the 5 s target. No provider or OpenRouter calls were made.

## Method and safety

The real local CACHE_ONLY Fetch API ran against Neon using only randomly named `ts_test_db06_*` schemas. Fixture setup and migrations were excluded from Fetch timings. Each request was followed through terminal `COMPLETED` and data-readiness checks; HTTP 202 was not treated as completion. All created schemas were dropped. Provider HTTP and OpenRouter calls were blocked and counted. No quota was reset, and no `trendscout` schema was written.

The SQL figure is execute/executemany client dispatches, not TCP packet round trips. Stage timings overlap for nested work; in particular, atomic-state and checkpoint coordination totals must not be added.

## DB06F changed-input results

The changed fixture changes content and observation time for the selected percentage while retaining all 1,500 stable keyword identities. Counts below are for the warm Fetch after its prime Fetch.

| Changed | Keyword new / updated / skipped | New observations | Source evidence inserted / updated / existing | Dataset evidence links added | Classifier calls | Payload bytes | SQL / tx | End-to-end |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1% (15) | 0 / 15 / 1,485 | 15 | 15 / 0 / 1,485 | 1,500 | 15 | 2,189,238 | 150 / 30 | 18.453 s |
| 10% (150) | 0 / 150 / 1,350 | 150 | 150 / 0 / 1,350 | 1,500 | 150 | 2,190,738 | 150 / 30 | 20.925 s |
| 100% (1,500) | 0 / 1,500 / 0 | 1,500 | 1,500 / 0 / 0 | 1,500 | 1,500 | 2,192,238 | 158 / 30 | 28.152 s |

Every case retained 1,500 provider records, keyword rows, source-evidence rows, distinct evidence-to-keyword links, and the user-pinned membership. A new durable dataset was created for changed input; it was not incorrectly reused. Algorithms remained `CACHE_ONLY`, AI remained `MODEL_UNAVAILABLE`, and provider/OpenRouter request counts were zero.

### Instrumented warm stages (milliseconds)

| Changed | Payload read/decode | Keyword persistence | Evidence persistence | Collection processing | Atomic state | Checkpoint coordination | Dataset finalization |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1% | 468.60 | 1,932.72 | 1,430.21 | 6,188.29 | 3,218.65 | 8,899.60 | 566.75 |
| 10% | 369.38 | 2,906.85 | 2,292.12 | 8,198.08 | 3,421.19 | 10,436.83 | 731.67 |
| 100% | 676.13 | 9,063.75 | 6,784.05 | 14,848.37 | 3,378.91 | 11,958.85 | 639.43 |

## Optimization and interpretation

Keyword persistence now compares stable IDs and writes only new, content-changed, or observation-only rows. Source evidence is identity-checked and only inserted when missing or updated when retention/lineage requires it; keyword and dataset links remain idempotent. Contextual classifications are reused by stored input digest when classification inputs are unchanged. Migration `0012_classification_input_digest.sql` adds the digest column. The 1% test exercised 1,485 skipped keyword writes, 1,485 existing evidence rows, and only 15 classifier calls; new evidence and observations were still kept.

A 100-record smoke was 6.095 s / 128 SQL dispatches / 28 transactions before selective persistence and 6.206 s / 131 / 29 after. It wrote 1 changed keyword and skipped 99; it retained all 100 records and lineage. The small fixture was inline, so source-payload-blob bytes were 0 in both smoke runs.

The latest historical 1,500-record changed baseline supplied for this task was median 98.800 s, approximately 2,094,486 payload bytes, 20–24 s Collections processing, 144 SQL dispatches, and 27 transactions. DB06F read approximately 2.19 MB and used 150–158 dispatches / 30 transactions. The selective row writes are verified, but the changed-path run did not reduce payload transfer, total dispatches, transaction count, or meet the latency goal. Collection processing and coordination remain the largest measured spans. The fixture payload differs from the historical 2.09 MB sample, so the byte comparison is directional rather than a same-fixture before/after.

## DB06E regression check

One post-DB06F 1,500-record warm unchanged sample completed in 3.115 s (min/median/max; one sample), with metadata-only fast path hit, 0 source payload bytes, 75 SQL dispatches, and 14 transactions. It reused the exact primed durable dataset ID and retained all 1,500 records, evidence rows and links, and the user membership. This is consistent with DB06E's 3.199 s median; unchanged fast-path code was not modified for DB06F.

## Validation and status

The targeted changed-input cases A/B/C and 100-record smoke passed terminal, data-count, lineage, membership, provider-block, and isolated-schema cleanup checks. Focused offline suite: 113 passed, 12 PostgreSQL tests skipped without the explicit test URL, 38 subtests passed. Four isolated-schema PostgreSQL regressions passed for incremental writes, source-ID remapping, retry idempotency, and retention lineage. Ruff passed for every changed Python file. P5 remains blocked because every 1,500-record changed-input case exceeds the 5 s target. No further optimization was attempted after the required measurements.

| Gate | Result |
|---|---|
| DB06E unchanged metadata-only reuse | PASS |
| DB06F changed-input row filtering and correctness | PASS |
| DB06F changed-input <5 s performance target | BLOCKED |
| P5 | BLOCKED |
