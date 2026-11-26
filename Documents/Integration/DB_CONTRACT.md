# TrendScout shared database contract

**Status:** Initial Database Goal contract, 26 November 2026. This records the
Database Goal's current ownership boundary and integration requirements for
the parallel AI, Algorithms, and Frontend goals. It is not evidence that those
goals have implemented or accepted these interfaces. Reconcile this document
with their contracts when those branches publish them.

## Ownership

The Database Goal owns PostgreSQL schema and migration order, connection and
runtime configuration, persistence adapters, quota and job coordination,
Fetch execution infrastructure, source cache/checkpoints, retention, scheduler
storage, and runtime composition. The other goals own their domain behavior:

| Goal | Domain ownership | Database integration requirement |
| --- | --- | --- |
| AI | Prompting, model selection, classification decisions, proposal/recommendation behavior | Persist through PostgreSQL-owned adapters. Store request identity, status, model/version, usage, timestamps, and provenance; do not put provider response bodies in logs or user-facing rows. |
| Algorithms | Normalization rules, candidate selection, feature definitions, scoring, rankings | Provide stable normalized identity, observation scope, input digest, algorithm/version, and evidence references. Keep collection time separate from provider observation time. |
| Frontend | User flows, presentation, client caching | Use versioned HTTP APIs only. Expose progress, per-source status/provenance, pagination, unavailable/stale states, and safe retry identifiers; never connect directly to PostgreSQL. |

## Shared persistence rules

- Neon PostgreSQL is the target runtime store. SQLite and Firestore remain
  migration sources until their data is reconciled, legacy writers are fenced,
  and the Integration Gate authorizes cutover.
- Use one configured database identity for Local and Vercel. Keep connection
  secrets in environment configuration; never place them in source, reports,
  API payloads, or logs.
- Access tables through a persistence adapter. Schema-qualify SQL and validate
  schema identifiers. A domain goal must not add a second database or direct
  browser database access.
- Store instants as timezone-aware `TIMESTAMPTZ` in UTC. Keep provider
  `observed_at` distinct from TrendScout `collected_at`; a date-only provider
  observation must not be converted to a fabricated instant.
- Persist source, collection time, provider observation time when available,
  query/sampling scope, geography, metric, unit, access mode, status, and
  provenance. Preserve zero, missing, unavailable, stale, partial, and rejected
  as distinct states.
- Quota, reservation, cooldown, refresh eligibility, and job lease transitions
  must be atomic and idempotent. Never infer quota by summing snapshots and
  never reset operational state during reconciliation.
- Persist large raw source bodies once in the source-evidence/payload boundary.
  Other tables should carry stable references, digests, and derived values,
  not untracked copies of provider content.
- Calculate `source_evidence.expires_at` with the versioned source-retention
  registry and the seven-local-date application limit, choosing the earlier
  deadline. Persist policy version, review status, and any provider maximum in
  `sources.configuration.retention_policy`. Unknown source IDs must fail
  lineage closed; a `partial` policy review is not evidence that provider terms
  have been fully mapped.
- New Google, Wikimedia Pageviews, and current Bing verification lineage may
  store a versioned list of `keywords.evidence` top-level field paths and hashes
  in `source_evidence.payload.retention_lineage`. The list contains no field
  values; a consumer must compare the current field digest before redacting it.
  Wikimedia entity-context rows may store a keyword ID, context version,
  collection instant, and whole-row digest in the same metadata object. This is
  forward lineage only: it does not certify historical rows or permit deleting
  source evidence while classification, algorithm, or saved-research copies
  remain untracked.
- When a data-mode collision maps a provider keyword to a mode-scoped ID, the
  persistence result carries that mapping. Source-evidence joins, their redaction
  descriptors, and Bing candidate updates must use the resolved ID so a LIVE
  observation cannot attach to the colliding SYNTHETIC row.
- PostgreSQL Fetch processing persists each successful non-Bing source's
  cleaned keyword batch and source-evidence links in one transaction. Bing
  verification updates are applied after candidate rows have been persisted;
  their lineage is written in that same transaction. The final dataset snapshot
  links these evidence rows to the Fetch. This is source-result persistence;
  provider page streaming remains a separate recovery requirement.

## Existing schema ownership map

The Neon target has migration versions 1–9 applied and checksum-verified.
Version 9 adds retention audit summaries. All 35 non-ledger tables remain
empty; the schema is a foundation, not a cutover-ready populated database.

| Data | Current table(s) | Owner and notes |
| --- | --- | --- |
| Sources and raw evidence | `sources`, `source_evidence`, `dataset_evidence`, `evidence_keywords` | Database owns storage and retention boundary; adapters own source mapping. `source_evidence.collected_at` and optional `expires_at` are the authoritative retention inputs. |
| Normalized keyword records | `keywords`, `memberships`, `feedback`, `keyword_decisions` | Algorithms own normalization semantics; Database owns identity constraints and persistence. Preserve user pins, decisions, and assignments when observation evidence expires. |
| Time-series observations and outputs | `algorithm_observations`, `algorithm_results` | Algorithms own metric/scoring definitions; Database owns typed persistence, lineage, and expiry enforcement. Existing schema still needs reliable collection-time and input-lineage coverage before automatic deletion is safe. |
| Collections and user research | `collections`, `research_records`, `proposals`, `suppression` | Preserve user-created records by age. Provider content embedded in saved records still follows provider retention; it must be separately attributable and redactable. |
| Classification/entity context | `classification_results`, `entity_context` | AI/Algorithms own decision semantics; Database owns persistence. Include model/rule version, source evidence references, and collection time for provider-derived fields. |
| Cache and resumable source state | `source_payload_blobs`, `source_payload_references`, `coordination_*` | Database owns atomicity, fencing, and expiry. Effective payload expiry is the earliest source, checkpoint, and job deadline. Preserve source-specific shorter limits, including YouTube. |
| Quota, jobs, and scheduler | `coordination_*`, `fetch_jobs`, `schedules`, `schedule_occurrences` | Database owns transactions, idempotency keys, leases/fences, quota continuity, and bounded recovery. User-facing jobs must expose stable states rather than database internals. |
| User settings and saved configuration | `settings` | Preserve unless an explicit user action or a separately approved policy says otherwise. Never purge solely by age. |

The actual business and operational table population has not been reconciled
into Neon. New goal branches must not treat this table map as proof of migrated
rows or as permission to switch runtime flags.

Known retention gap: legacy `keywords.evidence`,
`classification_results.source_evidence`, algorithm rows, and saved research
can contain provider-derived content without a trustworthy per-field
collection timestamp or source reference. The new cleanup reports due
`source_evidence` rows but keeps them until derived copies have lineage and a
safe invalidation/redaction path. Do not interpret a deleted blob or join row
as proof that all provider content copies have expired.

## Rolling retention contract

- Default policy: keep the seven local calendar dates `D-6` through `D`, using
  the proposed business timezone `Asia/Ho_Chi_Minh`. For example, on 26 Nov,
  retain collection dates 20–26 Nov. The cutoff is local midnight at the start
  of `D-6`, converted to UTC. Persist instants in UTC.
- For source content with a shorter provider limit, effective expiry is the
  earlier of the provider deadline and the seven-day policy deadline. A
  provider's shorter limit wins; no cache, including YouTube, may be extended
  by this default.
- Use collection time, not observation date or result-update time, to decide
  retention. Rows without trustworthy collection time or source lineage are
  unclassified and must be reported and retained until reconciled; do not
  invent timestamps to make them eligible.
- Expiry removes provider content and provider-dependent cached/derived copies
  while preserving user-created Collections, saved Research, settings,
  assignments, quota ledger, reservations, cooldowns, and refresh eligibility.
  Saved user text containing provider content needs field-level provenance and
  redaction; deleting the whole user record is not an acceptable shortcut.
- A cleanup batch must be idempotent, bounded, transactional, concurrency-safe,
  and must record an aggregate audit summary. It must support dry run, make no
  provider requests, preserve references or remove them in the same transaction,
  and expose remaining backlog for retries.
- Backups, AI outputs, caches, summaries, and derived records are subject to
  the same source expiry when they reproduce provider content. Keep provenance
  links so expiry can invalidate or redact dependent content.

## Schema and migration conventions

- Add one consecutive, four-digit, versioned SQL migration for each schema
  change. Never edit a migration after it has been applied; checksum mismatch
  is a hard failure.
- Prefer additive, nullable columns for legacy rows until provenance is
  reconciled. Do not use `now()` defaults to fabricate collection times for
  migrated data.
- New tables must define explicit ownership, primary/unique keys, foreign-key
  deletion semantics, indexes for bounded queries, and transaction boundaries.
  Destructive changes require an explicit reconciliation and rollback plan.
- Runtime code validates the exact bundled migration ledger and readiness.
  Migration application is an operator action, never an HTTP request side
  effect. Do not activate PostgreSQL while business or operational tables are
  empty or partially reconciled.
- Shared API/schema changes must be backward-compatible during rolling
  integration: additive response fields, stable existing meanings, explicit
  status enums, and versioned destructive/semantic changes. Database schema
  changes land before dependent writers; readers tolerate both old and new
  representations during migration.
- A migration/import must preserve source identity, timestamps and precision,
  provenance, user edits, quota and reservation state. Reconcile conflicts by
  domain semantics and evidence; never choose an entire database as winner by
  default.

## Current coordination status

At this contract's creation, only `origin/main` and the existing
`origin/codex/fix-d6-fetch1-recovery` branch were present remotely; the latter
is already contained in `origin/main`. No AI, Algorithms, or Frontend remote
branch contract was available to review. Local goal worktrees existed at the
`cf09b2e` baseline without uncommitted changes. Update this status after each
safe fetch and reconcile new contracts before merging shared interfaces.
