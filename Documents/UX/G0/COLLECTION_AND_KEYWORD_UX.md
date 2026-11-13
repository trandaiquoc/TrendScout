# Collections and keyword-workbench UX

**State:** Proposed end-state for later implementation. Current behavior remains documented in the audit.

## Workspace and states

Collections is the home for the durable keyword ledger and classification workflow. Desktop uses a collapsible Collection hierarchy/filter rail and main work area; mobile moves the tree into a filter drawer. Unclassified is a filter over actual records, not a separate database or a conclusion that classification is impossible.

Filters: Collection path; assignment (all, unassigned, local AI, user/manual, excluded); classification (classified, suggestion, ambiguous, unknown/insufficient context, not in taxonomy, needs review); source; observation date; metric availability; partial/cached/live provenance; query and sort.

Keep distinct states:

- `UNKNOWN`: insufficient context, not necessarily permanent.
- `AMBIGUOUS`: conflicting or weak evidence.
- `NOT_IN_TAXONOMY`: evidence may exist but no suitable branch.
- `SUGGESTED`: review is needed only when the configured mode requires it.
- User override/pin/exclusion takes precedence over AI replay.

## Keyword table

| Column | Meaning |
| --- | --- |
| Select | Checkbox; header selects visible page only |
| Keyword | Display form; original/canonical variants in detail |
| Topic | Collection path; preserve multiple memberships |
| Source | Provider/source and evidence count when multiple records exist |
| Evidence | Metric + unit + provider rank or derived result only when present |
| Last observed | Provider observation time, separate from Fetch time |
| State | Localized classification/assignment status; not color-only |
| Action | One Details action plus overflow menu |

Detail drawer includes all source/query relationships, original/normalized text, geography, metric/window/unit, provider observation timestamp, Fetch time, live/cache mode, partial/rejected flags, classifier evidence/version and user decision history. Deduplication must not erase source relationships.

## Batch workflow

1. Search/filter the full durable dataset.
2. Select page or “all N matching rows”; name the exact scope.
3. Choose Add, Copy, Move, Remove membership, Accept/Reject suggestion, Exclude, Restore or Reclassify (only where supported).
4. Select a valid target when required; disable invalid actions.
5. Confirm destructive/broad operations with impact and count.
6. Show successful/failed counts and per-record reason; retry only if safe/idempotent.

Inbox bulk assignment uses a blank translated placeholder until an explicit target is chosen. This preserves the corrected Windows behavior; no first Collection is silently selected.

## Reclassification and Collection lifecycle

Explain why a row is unassigned and whether retry is actionable: missing context, cached context unavailable, ambiguous evidence, no taxonomy fit, model unavailable, user suppression/exclusion, or stale classifier version. A retry names the evidence it will use and must not call a paid service/provider without authorization. Preserve feedback and pinned memberships. Merge/delete confirmation shows source/target and keyword impact. Deleting a Collection preserves source evidence and respects suppression.

## Existing contract

Current API supports Collection list/create/edit/delete/merge; inbox/classification lookup; memberships, bulk actions, organize/reclassification, mode, suggestion review, reconciliation rollback and job status. Durable search is `/api/v1/datasets/latest/keywords` with query/state/Collection/source/sort/page; bulk supports all-filtered actions. Current Overview owns the explorer while Collections has the classification inbox. G0 proposes consolidating them without changing contracts.

The 2026-11-13 report observed 154 eligible records, 125 classified, 11 ambiguous, 18 unknown, 29 unassigned, 180 LOCAL_AI membership rows across 125 assigned keywords, and 39 Collections. These are snapshot observations, not future targets.
