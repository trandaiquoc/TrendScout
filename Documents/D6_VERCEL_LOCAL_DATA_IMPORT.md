# One-time local data import to Vercel

The local LIVE Collections database was found at `%LOCALAPPDATA%\TrendScout\collections-v1\collections.sqlite`. Production currently has no saved keywords, so the database contains local-only data that can be imported into the existing Firestore snapshot.

The prepared tool is `Backend/scripts/import_local_collections_to_firestore.py`. It targets the existing `trendscout-192e4/(default)` database and reuses the local runtime's approved keyless impersonated client. It does not create credentials or change IAM.

Run from `Backend/`:

```powershell
python scripts/import_local_collections_to_firestore.py
```

The default is a dry run. Add `--apply` to commit after reviewing the per-table counts. Before applying, the tool writes a timestamped backup of the currently restored Firestore SQLite snapshot beside the local database, then merges with `INSERT OR IGNORE`. It aborts on incompatible table schemas, conflicting Collection or keyword identities, invalid SQLite integrity, or foreign-key errors. Snapshot version checks prevent a concurrent Firestore update from being overwritten.

The snapshot contract allows 20,000,000 expanded SQLite bytes and 2,900,000 gzip bytes. The local file is 72,351,744 bytes. An offline merge into a freshly initialized LIVE store, omitting `cache`, `algorithm_results`, and local `settings`, produced 10,498,048 SQLite bytes and 1,612,836 gzip bytes. The actual Firestore snapshot still needs a credentialed dry run to confirm its merged size and identities. Existing target rows remain authoritative when a row key already exists. All other allowlisted Collections and user research tables are included.

This import has **not** been run: this machine currently lacks the approved keyless impersonated ADC. The first attempt reported no Google Application Default Credentials, and no IAM or credential setup was performed.
