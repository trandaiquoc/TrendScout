# YouTube Data Deletion Runbook

**Status:** Local operator workflow prepared; public contact, receipt retention, and publication remain pending owner review.

**Last checked:** 16 November 2026

This runbook covers YouTube API Data in the current local TrendScout stores. It is not a public deletion form or a promise that TrendScout can delete provider-side data. The [YouTube Developer Policies](https://developers.google.com/youtube/terms/developer-policies/) require applicable API Data to be deleted or refreshed within the provider's retention limit; the application is configured with a seven-day cache ceiling and a one-hour Fetch checkpoint lifetime.

## Current storage boundary

- The shared source cache, Fetch jobs, YouTube checkpoints, quota reservations, and YouTube refresh state use `state.sqlite` under `%USERPROFILE%\.local\state\trendscout\normal-google-live-v1` on this Windows setup.
- The Collections database under `%LOCALAPPDATA%\TrendScout\collections-v1\collections.sqlite` stores no YouTube videos as keywords or scoring observations. It may store a per-source Fetch manifest and counts. New manifests retain exact source-level provider and received counts so a mixed-source summary can be adjusted safely.
- The YouTube retention sweep removes expired YouTube checkpoints and their expired blob only when no remaining checkpoint references it. It does not touch another source's checkpoint, quota, schedule, or cache entry.
- There are no user accounts. A deletion request is scoped to the local TrendScout installation, not a uniquely identified remote account. Browser local storage and provider-side records are outside the local database workflow.
- The application does not create automatic backup files. Manual backups, redirected console output, and copies outside the normal TrendScout directories require separate operator review.

## Receive and verify a request

1. Receive the request through the public contact channel approved by the owner. Do not ask the requester to send API keys, video IDs, passwords, or other credentials.
2. Verify the requester through an independently known contact channel. TrendScout has no account or login record that can verify identity automatically.
3. Stop the local TrendScout application and any process using the same state database. Do not run the deletion while a YouTube Fetch is queued or running.
4. From `D:\TrendScout\Backend`, inspect the local impact without changing data:

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.youtube_deletion preview
   ```

5. Record a minimal local request ticket. The command stores only a hash of its one-time reference and a receipt timestamp; it does not store the requester's name, email, video IDs, or request text:

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.youtube_deletion receive
   ```

   Keep the printed opaque reference in the operator's approved private case record. Do not paste personal details into the reference field.

## Fulfill and verify

Run the command with the received reference. It prompts for requester verification, confirmation that the application is stopped, and the exact deletion phrase:

```powershell
.\.venv\Scripts\python.exe -m scripts.youtube_deletion fulfill --request-ref <OPAQUE_REFERENCE>
```

The operation is idempotent for a completed ticket. It removes the current YouTube-scoped cache entry, all YouTube source checkpoints, unshared YouTube checkpoint blobs, YouTube-only Fetch summaries, and YouTube fields/counts from mixed-source summaries. It preserves non-YouTube checkpoint data, source records, collections, quota reservations, refresh schedules, and unrelated cache entries.

If the process stops after purging the source-state database but before updating the separate Collections database, the request remains at `STATE_PURGED`. Rerun the same `fulfill` command after resolving the reported issue; the first database is not purged a second time. A mixed-source legacy manifest without source-level counts fails closed and needs manual review before the request can be marked complete.

After the command completes:

1. Run `preview` again and confirm the YouTube cache/checkpoint/manifest counts are zero for the handled databases.
2. Reopen the SQLite databases read-only and verify `PRAGMA integrity_check` returns `ok`; retain unrelated Google, Bing, and Wikimedia records and all quota/schedule state.
3. Ask the requester to clear the local site's browser data if they want the saved policy-version acceptance and display preferences removed. TrendScout cannot clear storage on their device remotely.
4. Review every manual backup and any redirected console output separately. Do not claim deletion is complete for a backup or console copy that was not inspected. The current operator tool accepts explicit private database paths for recognized backups:

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.youtube_deletion preview --state-db "<PRIVATE_BACKUP>\state.sqlite" --collections-db "<PRIVATE_BACKUP>\collections.sqlite"
   ```

   If YouTube data is present, receive a ticket in that backup database and run `fulfill` with the same `--state-db` and `--collections-db` arguments. The tool refuses insecure state directories and malformed mixed-source summaries. Keep the backup private while the request is pending and verify the cleaned copy before restoring or replacing an archive.

The local request receipt stores a one-way ticket hash, timestamps, and deletion counts only. Its ledger is capped at 512 requests; automatic receipt aging is not implemented and the retention duration remains an owner decision. External YouTube data is not deleted by this workflow.

## GitHub Pages preparation

Repository Pages is not currently configured. The local repository contains a manual-only workflow at `.github/workflows/publish-policy-pages.yml`; it packages only `privacy.html`, `terms.html`, `policy.css`, and `.nojekyll`. It has no push trigger and refuses to deploy while a draft marker or owner placeholder remains. GitHub documents the Pages Actions source and artifact/deploy flow in its [custom workflow guide](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

The workflow uses `actions/checkout@v7`, `actions/upload-pages-artifact@v4`, and `actions/deploy-pages@v5` as listed by the official action repositories on 16 November 2026 ([checkout releases](https://github.com/actions/checkout/releases), [upload-pages-artifact releases](https://github.com/actions/upload-pages-artifact/releases), [deploy-pages releases](https://github.com/actions/deploy-pages/releases)). The upload step includes hidden files so `.nojekyll` is part of the policy-only artifact.

After reviewing and approving both documents:

1. Replace the owner/contact placeholders, settle the deletion-receipt retention period and legally consequential Terms clauses, and set the effective date.
2. Commit and push the approved documents and workflow through the owner's normal review process.
3. In repository **Settings → Pages → Build and deployment**, select **GitHub Actions** as the publishing source.
4. In **Actions**, choose **Publish policy pages (manual)**, select `main`, review the exact commit, and set `owner_approved` to `true` only after approving the text.
5. Confirm the workflow's published page URL and verify the two pages, relative links, HTTPS provider links, mobile layout, and that the deployed artifact contains only policy files.
6. Only then use the verified public page URLs in the application and separately approve the YouTube policy switch.

No page URL is currently published or verified. Do not enable YouTube based on a local URL, a draft, or a successful workflow file check.
