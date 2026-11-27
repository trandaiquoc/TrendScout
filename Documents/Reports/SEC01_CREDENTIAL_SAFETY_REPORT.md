# SEC01 — Credential Safety Regression

**Reviewed:** 2026-11-28 · **Branch:** `integration/demo-final` · **Recovery baseline:** `edfc5fb`

**Overall:** [100%] PASS — G1–G6 PASS. The Owner explicitly confirmed Neon password rotation, invalidation of the old credential, and update of the root `.env`; the incident is closed.

## Acceptance gates

- **G1 — PASS.** Audited the direct and pooled PostgreSQL connection setup paths, pool warning logs, and pytest failure display behavior. Driver connection messages and traceback context were the credential exposure boundary addressed here.
- **G2 — PASS.** Connection failures now retain only exception class, validated SQLSTATE, and a fixed allowlisted failure category. Direct connect, pool initialization, and pool acquisition raise sanitized errors without chained driver context. Pool warning records discard raw message arguments, exception text, and stack details.
- **G3 — PASS.** Regression tests use dummy connection URLs and tokens for direct and pooled failures and assert that the diagnostic class, SQLSTATE, and safe category remain available while dummy credentials do not appear.
- **G4 — PASS.** Pytest locals are disabled by repository configuration and enforced even when invoked with `--showlocals`. Dummy connection URLs and token patterns were absent from the test diagnostics and Markdown reports. No previously exposed credential was reproduced.
- **G5 — PASS.** The owner-provided root `.env` values authenticated with TLS verification on both direct (`DATABASE_URL_UNPOOLED`) and pooled (`DATABASE_URL`) connections. Direct and pooled connections resolved to the same normalized endpoint/branch, cluster, database, and role. The inherited process environment did not override either root `.env` value. A uniquely named `ts_test_sec01_*` schema was created, verified, dropped, and confirmed absent inside a direct-connection transaction. No application or production schema data was read or changed.
- **G6 — PASS.** In the user-provided Owner confirmation in this session, the Owner states that the Neon password was changed, the old credential was disabled, and the new connection strings were placed in root `.env`. The Owner also confirms pooled and direct TLS authentication passed. This satisfies the outstanding rotation/invalidation criterion. No credential value was reproduced, no `.env` or Vercel environment value was changed by this task, and no additional rotation was requested or performed.

## Changes and verification

- Added safe PostgreSQL connection failure classification and removed raw driver diagnostics from connection errors and pool warning logs.
- Added regression coverage for direct and pooled failure paths, log redaction, preserved safe diagnostics, suppressed exception context, and pytest locals.
- `Backend/tests/test_postgres_connection.py --showlocals`: **9 passed**.
- Direct and pooled TLS authentication, database/branch/role identity, and isolated-schema lifecycle: **PASS**.
- Owner rotation/invalidation confirmation: **PASS**; no old or new credential value recorded.
- Ruff on changed Python files: **PASS**. `compileall`: **PASS**. `git diff --check`: **PASS**. Markdown secret-pattern scan: **CLEAR**.

## Limitations

This change prevents the audited connection diagnostics from echoing connection URLs and credentials. The updated root `.env` values work, and the isolated check was removed cleanly. The Owner's explicit confirmation satisfies the final incident-closure requirement.
