# SEC01 — Credential Safety Regression

**Reviewed:** 2026-11-27 · **Branch:** `integration/demo-final` · **Requested baseline:** `b1bb1ec`

**Overall:** [67%] BLOCKED — G1–G4 PASS; G5 is blocked by authentication failure on the repository-root PostgreSQL credential; G6 remains pending owner rotation confirmation. The incident is **not closed**.

## Acceptance gates

- **G1 — PASS.** Audited the direct and pooled PostgreSQL connection setup paths, pool warning logs, and pytest failure display behavior. Driver connection messages and traceback context were the credential exposure boundary addressed here.
- **G2 — PASS.** Connection failures now retain only exception class, validated SQLSTATE, and a fixed allowlisted failure category. Direct connect, pool initialization, and pool acquisition raise sanitized errors without chained driver context. Pool warning records discard raw message arguments, exception text, and stack details.
- **G3 — PASS.** Regression tests use dummy connection URLs and tokens for direct and pooled failures and assert that the diagnostic class, SQLSTATE, and safe category remain available while dummy credentials do not appear.
- **G4 — PASS.** Pytest locals are disabled by repository configuration and enforced even when invoked with `--showlocals`. Dummy connection URLs and token patterns were absent from the test diagnostics and Markdown reports. No previously exposed credential was reproduced.
- **G5 — BLOCKED.** A direct TLS connection using only the repository-root `.env` failed during authentication (`OperationalError`, sanitized category `authentication_failed`). No isolated schema was created, no schema was modified, no credential value was read or printed, and no retry was made after the authentication failure. The required isolated-schema check therefore could not complete.
- **G6 — PENDING.** Credential rotation is the owner's responsibility. Rotation confirmation has not been provided; no `.env` or Vercel environment value was changed. The security incident cannot be declared closed until the owner confirms remediation and the isolated connection check succeeds.

## Changes and verification

- Added safe PostgreSQL connection failure classification and removed raw driver diagnostics from connection errors and pool warning logs.
- Added regression coverage for direct and pooled failure paths, log redaction, preserved safe diagnostics, suppressed exception context, and pytest locals.
- `Backend/tests/test_postgres_connection.py --showlocals`: **9 passed**.
- Ruff on changed Python files: **PASS**. `compileall`: **PASS**. `git diff --check`: **PASS**. Markdown secret-pattern scan: **CLEAR**.

## Limitations

This change prevents the audited connection diagnostics from echoing connection URLs and credentials. The root database credential did not authenticate, so its current validity, rotation state, and access to an isolated schema remain unverified. Credential rotation and incident closure remain with the owner.
