# DB07 PostgreSQL Consolidated Regression

Date: 2026-11-27

Branch: `integration/demo-final`

Baseline: `a26c2992dd28f16922d77645b3796dc21e101f38`

- **Migration chain:** PASS. Versions 0001–0012 passed fresh-schema, 0009→0012 upgrade, checksum-ledger, rollback, and idempotent reapply checks in isolated schemas; test-owned schemas were dropped by cleanup.
- **Schema compatibility:** PASS. Legacy imported dataset data remains readable; new processing status fields default to `UNKNOWN`, and classification input digest column is present.
- **PostgreSQL unavailable:** PASS. Runtime tests confirm fail-closed behavior without SQLite or Firestore fallback.
- **DB06 regression:** metadata-only reuse, selective persistence, classification digest, dataset processing statuses, and connection pooling contracts passed in the consolidated suite or focused retests.
- **Consolidated run:** 380 passed, 20 failed, 0 skipped, 151 subtests; 37 modules. `.env` PostgreSQL test URL was used; no provider/OpenRouter credentials were available.
- **Database/config fixes:** PostgreSQL proposal reads now return stored `source_ids`; PostgreSQL runtime requires explicit `TREND_SCOUT_RUN_MODE`; migration-version and additive-status compatibility assertions now match schema 0012.
- **Focused verification:** 10 DB/runtime/process-pool checks passed after fixes; classification digest test passed. The five process-pool failures in the consolidated run were caused by stdin-based Windows test startup and passed via `pytest.exe`.
- **Smart Collections:** 6 failures were non-DB: 2 algorithm-cache expectations and 4 AI availability/status expectations under the no-credential run. Deferred to AI/Algorithm work; no inference ran.
- **Algorithms:** 5 algorithm contract failures remain unrelated to PostgreSQL and are deferred; no algorithm behavior was changed.
- **Ruff:** changed Python files PASS. Repository-wide Ruff reports 1,518 findings in unchanged files. `compileall` and `git diff --check` PASS.
- **Runtime ENV:** PostgreSQL requires `TREND_SCOUT_POSTGRES_RUNTIME_ENABLED=true`, explicit `TREND_SCOUT_RUN_MODE=live`, pooled and direct URLs to the same database, and an exact migration ledger.
- **Quota:** Production `UNKNOWN` quota count remains unverified; only isolated schemas were used, as required.
- **Legacy writer fencing:** fenced-state and fail-closed contracts pass in tests; operational confirmation that every legacy writer is stopped remains a cutover prerequisite.
- **Backup/rollback:** migration transaction rollback is tested; production backup and restore rehearsal were not performed.
- **Cutover readiness:** BLOCKED pending quota evidence, legacy-writer freeze confirmation, and verified backup/rollback. No production schema or environment was changed; no production migration or cutover occurred.
- **Performance:** DB06 P5 remains BLOCKED; DB07 did not rerun DB06 benchmarks or change performance code.
- **DB07:** PASS for the PostgreSQL regression scope; non-DB AI/Algorithm failures are recorded above for later work.
