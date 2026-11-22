# D6 Vercel Fetch All — báo cáo sửa lỗi

**Ngày:** 2026-11-19
**Nhánh làm việc:** `main`
**Trạng thái:** G1–G6 PASS.
**Tiến độ:** 100% theo trọng số gate.

## Phạm vi và giới hạn

Sửa tính bền vững của Fetch dùng Firestore, khôi phục job Vercel bị kẹt, và UX sau reload. Không gửi POST Fetch, không gọi API nhà cung cấp thật, không reset dữ liệu/quota Production, không sửa IAM/billing.

**Production deployment đã xảy ra:** ban đầu, theo yêu cầu dùng một link chính, Preview `dpl_6WFCatRwF5ZByMeMGJ76Dgb4kcPw` được redeploy qua Vercel API thành `dpl_7b6cFCmW4qXCJgNuEe4Wquw6XzYV`. Metadata ghi `source: redeploy`, không có Git commit SHA; source lúc đó là worktree local tải lên Vercel. Sau đó D6 được commit/push thẳng lên `main` (`6f374e5`); workflow timeout được cập nhật ở `36a380e`. Deployment dùng khi Owner nghiệm thu là Git deployment `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F` từ commit code `36a380e`. Report-only commit `83a613e` sau đó cũng được Vercel tự build thành deployment Git Production `dpl_FQFycNry5azPUTB8aXCR3nyU6yHE` ở trạng thái `READY`; không có thay đổi app code trong commit đó. Alias Owner vẫn chỉ có một URL.

Run đã được báo cáo: `14e4f63bc0944b65b573d0b23a787c8e`. Vercel vẫn trả thông tin trạng thái từ shared state, nhưng runtime logs của run này không còn trong thời hạn lưu hiện tại. Vì vậy không thể xác nhận duy nhất lỗi nào đã xảy ra trong request Production cụ thể. Các nguyên nhân bên dưới là failure mode được tái hiện độc lập trên code hiện tại, không phải kết luận về log đã hết hạn.

## Phát hiện và thay đổi

1. **Checkpoint commit có kết quả không rõ:** Firestore có thể commit transaction nhưng client mất acknowledgement. `SourceCheckpointJournal.save()` nay thử lại cùng snapshot, revision và digest tối đa ba lần. Nếu lần đầu đã commit, kiểm tra revision+digest nhận diện bản đã lưu; không gọi lại provider.
2. **Giới hạn snapshot không khớp:** checkpoint cho phép payload đã giải quyết đến 5.000.000 byte nhưng blob adapter giới hạn 4.000.016 byte. Giới hạn blob được nâng lên 5.000.000 byte và test round-trip fixture 4,1 MB qua nhiều chunk. Theo tài liệu Firestore chính thức đã kiểm tra ngày 2026-11-19, mỗi document tối đa 1 MiB và API request tối đa 10 MiB; chunk hiện tại 196.608 byte giữ từng document dưới giới hạn. Việc kiểm tra tài liệu không xác nhận project credentials hay request Production cụ thể. [Firestore quotas and limits](https://docs.cloud.google.com/firestore/quotas)
3. **Job `RUNNING` hết lease:** đọc status/history nay sửa lease hết hạn thành `QUEUED` kèm `recovery_available`, hoặc đặt trạng thái terminal khi hết deadline/số lần thử. GET không dispatch worker. Nếu còn kết quả nguồn đã checkpoint, job kết thúc `PARTIAL` để giữ phần đã lưu. Checkpoint giữ theo hạn retention riêng thay vì bị cắt theo deadline thực thi job.
4. **Reload và tiếp tục có kiểm soát:** dashboard render capabilities gần nhất từ cache trình duyệt, ghi rõ trạng thái cũ và cho thử lại khi request lỗi. Cache không chứa Fetch ID, kết quả, credential hay dữ liệu riêng. Reload chỉ GET. Run History có nút **Tiếp tục Fetch** cho job đã phục hồi; thao tác POST chỉ xảy ra khi người dùng bấm, gửi lại scope nguồn trong lịch sử để server nhập lại job queued và tái sử dụng checkpoint.
5. **Phạm vi lịch sử khôi phục:** shared history nay giữ `request_kind` và `history_days`, ngoài danh sách nguồn, để UI tiếp tục đúng loại job và cửa sổ lịch sử.

## Kiểm tra

- **150 backend tests PASS** khi chạy lại trên Python của `Backend/.venv`: 119 test qua `test_d5_source_checkpoints`, `test_d5_public_admission`, `test_d5_distributed_worker`, `test_d5_firestore_shared_cache`, `test_google_trends`, `test_bing_handler`, `test_vercel_shared_live_fetch`; thêm 31 test qua `test_d5_guarded_attempt`, `test_d5_live_gateway`, `test_d5_firestore_atomic_state`.
- **26 Firestore Emulator integration tests PASS** trên GitHub Actions run [44542548880](https://github.com/trandaiquoc/TrendScout/actions/runs/44542548880), commit `36a380ed90ce0ca3408b8ae23ad6d3e620cddf59`. Chạy `test_firestore_transactional` và `test_firestore_shared_runtime`; gồm Google Fetch route với RSS fixture, cache/quota/checkpoint persistence, nhiều client và lost-commit retry. Không gọi Google/provider thật.
- **24 frontend tests PASS** qua `fetch-loading-state`, `fetch-source-selection` và `run-history-coverage`.
- **6 browser modules** được kiểm tra cú pháp dưới ES modules.
- `Backend/scripts/prepare_vercel.py` chạy thành công. Bản chạy lại không in số lượng assets.
- Vercel Preview `https://trend-scout-np8wty5kt-rakoon1.vercel.app` ở trạng thái `READY`, `target: null`; build dùng cây làm việc hiện tại, không push hoặc tạo nhánh. GET tĩnh `/dashboard/` và các module `fetch.js`, `run-history.js` đều trả **HTTP 200**; HTML có `lang="vi"`, module Run History có trường và nhãn recovery.
- Playwright render Preview bằng share link tạm, với mọi `/api/**` bị chặn ngay trong browser và trả mock `503`. Shell tiếng Việt và nút thử lại hiển thị; không có request app API tới server. Ở viewport 390 px, `scrollWidth = clientWidth = 390`, không tràn ngang. 7 console errors đều là HTTP 503 do mock có chủ ý.
- URL Owner cung cấp `https://trend-scout-mu.vercel.app` ban đầu là alias **Production** ở commit cũ `b57389babc6ea308043a31c9ef66a935da3b269d` (deployment `dpl_2r294JjWURedcsGhwiaAB8zDDbmZ`). Theo chỉ dẫn của Owner dùng một link, bản D6 đã được phát hành lên alias đó.
- Production deployment dùng khi Owner nghiệm thu là `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F`, `target: production`, Git build từ commit code `36a380e`. Vercel web fetch xác nhận HTML và module `fetch.js`, `run-history.js` trên URL Owner đưa trả **HTTP 200**; module Run History có recovery field/label. Đây là kiểm tra tĩnh, không xác nhận UI chạy cùng API thật.
- Owner manual acceptance **PASS** trên URL Production duy nhất `https://trend-scout-mu.vercel.app/dashboard/`: reload trang, dashboard tiếng Việt hiển thị, không kẹt ở “Đang kiểm tra”, và Run History tải dòng job đã lưu với bốn nguồn. Production GET `/api/v1/runs/history?limit=20` trả **HTTP 200**, có run `14e4f63bc0944b65b573d0b23a787c8e`, trạng thái `PARTIAL`, đủ Bing, Google, Wikimedia và YouTube; response báo `history_source=local_sqlite_and_shared_firestore`. Đây là bản ghi cũ đã lưu, không phải kết quả của Fetch mới. `manual_history_status=LOCAL_LEDGER_ONLY` chỉ áp dụng lịch sử `MANUAL_ANALYSIS`; mục Fetch vẫn có trong Run History.
- Truy vấn runtime logs mới nhất bị Vercel API từ chối `403`; tình trạng log runtime hiện **chưa xác minh**, không kết luận rằng không có lỗi.
- Playwright mở trực tiếp link Production bị chuyển tới trang đăng nhập Vercel. Owner đã kiểm tra UI trong phiên đăng nhập của mình. Không tắt Deployment Protection hay thay đổi quyền project.
- JavaScript dashboard tự gửi GET capabilities/history khi chạy bình thường. Trong Playwright Preview smoke, mọi route `/api/**` bị chặn tại browser và mock 503 nên browser smoke không gửi request app API thật. Riêng QA Production gọi GET history chỉ đọc và nhận HTTP 200; không gửi Fetch POST hay gọi provider.
- `git diff --check` sạch. Các kiểm tra local dùng fixture/transport giả và SQLite tạm; Firestore Emulator được chạy trong runner GitHub Linux cô lập với `bwrap` và network chỉ-loopback vì Windows 1 không có Java/WSL.
- GitHub Actions Firestore Emulator run `44542548880` và Pages build run `44542547219` đều **success** trên commit `36a380ed90ce0ca3408b8ae23ad6d3e620cddf59`.
- Code D6 và các cập nhật workflow đã được push trực tiếp lên `main`, không tạo nhánh công việc. Các commit sau nghiệm thu chỉ cập nhật báo cáo; Vercel Git tự build commit trên `main`.

## Gate

| Gate | Trọng số | Trạng thái | Bằng chứng |
|---|---:|---|---|
| G1 — tái hiện và khoanh vùng | 15% | PASS | Bốn failure mode được tái hiện bằng test trước khi sửa. Nguyên nhân của run Production cũ vẫn chưa xác minh được vì log hết hạn. |
| G2 — persistence/Firestore | 25% | PASS | Retry checkpoint idempotent, chunk snapshot 4,1 MB, retention độc lập với deadline. |
| G3 — lifecycle/recovery | 20% | PASS | Lease hết hạn, deadline, checkpoint đã hoàn tất, status/history không dispatch, POST tường minh để tiếp tục. |
| G4 — frontend/reload UX | 15% | PASS | Capabilities cache có nhãn stale; Run History tiếp tục theo scope lưu; reload không tự POST. |
| G5 — tích hợp/hồi quy | 15% | PASS | 150 backend, 24 frontend và 26 Firestore Emulator tests đạt; GitHub Actions run trên commit `36a380e` success. |
| G6 — Vercel QA + Owner acceptance | 10% | PASS | Production deployment `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F` từ commit `36a380e` `READY`; API history trả 200 và Owner xác nhận UI/history PASS sau reload trên alias chính. Không gửi Fetch mới hay gọi provider thật. |

## Kết luận nghiệm thu

G1–G6 đều PASS. Vercel Production chạy code đã push lên `main`; Owner xác nhận dashboard và Run History sau reload. Không gửi Fetch POST, gọi provider thật, reset Production data/quota hay thay đổi IAM/billing trong quá trình QA. Bản ghi Production hiện có mang trạng thái `PARTIAL`; việc hiển thị đúng trạng thái đã lưu được nghiệm thu, còn một lần Fetch live mới chưa được chạy.

## Production deployment and rollback

- The Production deployment used for Owner acceptance was `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F`, built by Git from app-code commit `36a380e` and assigned to the single Owner-provided alias. Report-only commit `83a613e` was later auto-deployed by Vercel as `dpl_FQFycNry5azPUTB8aXCR3nyU6yHE`, `READY`. No Fetch POST or provider request was sent during deployment verification.
- Rollback target is previous Git-backed Production deployment `dpl_8KHV54j2qC74eUQCmzJAHKsDEroS` at D6 code commit `6f374e5aa8cc5a3c0eb64ef810cad82432429652`. If a release issue appears, use Vercel `request_rollback` for project `prj_dB1JIG2hJG8NUukGPAaqvyI1Pk3R` with that deployment ID, then verify `trend-scout-mu.vercel.app` points back to it. No rollback request was sent.

## Owner follow-up — Fetch All retry report (2026-11-23)

**State:** focused checks pass and the new Git Production deployment is READY; Owner's live Fetch All acceptance is pending. This follow-up does not change the historical G1–G6 result above and does not claim the current Fetch All flow has passed live acceptance.

### Owner report and verified Production evidence

Owner reported that Fetch All stayed on “Đang gửi Fetch LIVE tới Vercel…” for more than five minutes, then showed “Chưa rõ máy chủ đã nhận yêu cầu. Thử lại sẽ dùng cùng khóa yêu cầu.” Repeating the button did not change that message. The dashboard summary shared by Owner showed 47 provider records, 0 unique keywords, 0 classified keywords, and a PARTIAL run from 2026-11-19.

Read-only Production API checks on 2026-11-22 found the old Git deployment `dpl_7Q3EeJebTwZFur9ueK7Woj54GdMX` (commit `aa77764c3fd493b8e59f0cbe7a53e81ab664d0d3`). `GET /api/v1/runtime` returned 503 `COLLECTIONS_UNAVAILABLE`; `GET /api/v1/analytics/overview` returned 200 with 929 saved unique keywords, 79 classified and 850 unclassified. Its latest snapshot was PARTIAL: 1,046 provider records received, 929 retained, and `MODEL_UNAVAILABLE`. The source outcomes were Wikimedia SUCCESS/CACHED (999 records), YouTube PARTIAL/STALE_CACHE (47 of 50 provider records retained), Google UNAVAILABLE/NOT_YET_ELIGIBLE (0 provider calls), and Bing UNAVAILABLE/NO_SAVED_CANDIDATE (0 requests).

The Owner's dashboard summary and the read-only overview disagree (47 received versus 1,046, and 0 versus 929 retained). Their scope or timing has not been resolved. The old Production runtime logs did not retain the reported POST, so its exact HTTP failure cannot be attributed to a specific request. No new Fetch POST or provider call was sent during this follow-up.

### Confirmed code-path failures and changes

The exact historical Production request remains unconfirmed, but review of the deployed code confirmed these failure modes:

1. The Fetch POST timeout was 65 seconds while `Backend/vercel.json` caps each Function at 60 seconds.
2. Uncertain initial POSTs shared the source continuation allowance of eight attempts. A same-key replay also started a source slice without distinguishing a lost initial response from an intentional continuation. Together these paths could keep the UI waiting for minutes before a job ID reached the browser.
3. Most definite client errors were presented as an uncertain server acknowledgement, and an HTTP-success response with no valid Fetch ID could be polled as `undefined`.
4. Exact `GET /api/v1/runtime` was missing from the Firestore request-scoped route list, even though its handler reads `CollectionService`.
5. Vercel Fetch attempted local embedding inference inline. Lazy FastEmbed initialization could download the model during a bounded Fetch invocation.

The follow-up changes now bound Fetch POSTs to 55 seconds, allow one automatic retry for an uncertain submission with the same idempotency key and no continuation marker, and require a valid 32-character Fetch ID before polling. Only a replay explicitly marked `X-TrendScout-Fetch-Continuation: 1` starts one source slice. Definite 4xx errors are shown as rejections; a corrected attempt uses the current source selection and a fresh key. Existing automatic job continuations stay bounded and keep the durable source checkpoints.

The exact runtime GET now uses the request-scoped Firestore context. Vercel Fetch collection processing stores the raw keyword snapshot and records `MODEL_UNAVAILABLE` with `MODEL_INFERENCE_DEFERRED` without loading FastEmbed. Run History keeps provider status separate from Collection processing status and exposes the processing error code in Vietnamese and English.

The pre-existing four-source rules remain in place: Google is admitted before Bing when Bing needs a saved candidate; saved source caches and original timestamps are preserved; YouTube still requires the approved policy version and does not become a keyword source. The focused Vercel source tests continue to use fake adapters and do not call providers.

### Local validation and data migration

- Focused backend regression: 87 passed, 10 subtests passed across `test_vercel_shared_live_fetch.py`, `test_smart_collections.py`, `test_keyword_workbench.py`, and `test_normal_wikimedia_live.py`.
- Frontend suite: 49 passed. After the final Fetch ID regex adjustment, the affected Fetch suite was rerun: 27 passed.
- Ruff, Python compile, JavaScript syntax and `git diff --check`: passed. `prepare_vercel.py` packaged 22 static assets and included the updated Fetch, Run History and localization modules.
- Local LIVE SQLite remains 72,351,744 bytes; `integrity_check=ok`, no foreign-key errors, 2,665 keywords, 62 Collections, 399 memberships, 24 dataset snapshots and 29 Fetch jobs.
- The first Firestore import dry-run on 2026-11-23 stopped before snapshot access with `DefaultCredentialsError`. After the Owner signed in through the already-approved keyless ADC impersonation, the read-only dry-run succeeded against Firestore snapshot version 4. Schema, SQLite integrity, foreign-key, and keyword/Collection identity checks passed. Planned inserts: 1,736 keywords, 49 Collections, 399 memberships, 24 dataset snapshots, 1,994 algorithm observations, 1,736 classification results, 3,762 reconciliation items, 29 Fetch jobs, 16 reconciliation runs, 211 entity-context rows, and 1 research record. It omits 31,980 recomputable algorithm results, 6,044 embedding-cache rows, and one local settings row. The import script committed nothing; the local database remains unchanged. Codex made no IAM or credential changes.
- Applying the local database merge remains prohibited until the Owner separately confirms `--apply` after reviewing the dry-run counts. The read-only GETs still show 929 existing Production keywords, so this is a merge into existing Firestore data.

Commit `ec6ea64f82de2d5012b10e7772b432df7f1bef62` was pushed directly to `main`. Vercel Git deployment `dpl_ES9NBL5sFWaCpv4aRwaQaE4dATtM` is `READY`, built from that exact SHA, and owns `trend-scout-mu.vercel.app`.

Read-only Production verification returned HTTP 200 for `/`, `/dashboard/`, `/api/v1/runtime`, `/api/v1/analytics/overview`, `/api/v1/datasets/latest`, `/api/v1/datasets/latest/keywords?limit=5`, `/api/v1/collections`, `/api/v1/runs/history?limit=5`, `/api/v1/research`, `/api/v1/sources/library`, `/api/v1/algorithms`, and `/api/v1/algorithms/results?limit=5`. Both HTML routes returned the current dashboard with `lang="vi"`. The runtime body reported storage `AVAILABLE`, integrity `OK`, 929 saved keywords, and `model_load_status=DISABLED_FOR_FETCH`. Overview and latest-dataset responses retained the existing PARTIAL snapshot and its source-level outcomes; the keyword route returned saved keyword rows, Collections returned its stored taxonomy data, Research returned a valid empty list, and Algorithms returned persisted result summaries. The responses did not initiate Fetch or call a source provider.

The user's earlier displayed 47-record/0-keyword summary disagreed with the saved 1,046-received/929-retained snapshot. The Owner later authorized Codex to complete the live acceptance within two user-initiated Fetch All runs.

### Production Fetch All acceptance (2026-11-23, Asia/Bangkok)

Production commit `2c3ab2ed06c1caecdcfe0514c21d8f52064bd7fb` was deployed as `dpl_CmMgefgFYFaSSTLsdD3AHfGBNeCE` (`READY`) to the existing alias `https://trend-scout-mu.vercel.app`. After reload, `/api/health`, `/api/v1/capabilities`, `/api/v1/datasets/latest`, `/api/v1/analytics/overview`, `/api/v1/collections`, and `/api/v1/runs/history` returned HTTP 200 in the authenticated browser session. Dashboard and Run History remained usable. A fresh reload eventually showed the latest persisted summary: 1,047 provider records received, 1,309 unique keywords, 92 classified and 1,217 unclassified.

Exactly two user-initiated Fetch All LIVE runs were started; client retries/continuations reused the same idempotency key and fetch ID for each run:

| Run ID | Bangkok time | Result |
|---|---|---|
| `ec598fbf8fc684fbb8f74b724e2d92dc` | 03:11–03:16 | `PARTIAL`; Wikimedia `SUCCESS/FRESH`, 1,000 records and 1 provider request; Bing `UNAVAILABLE/NO_SAVED_CANDIDATE`; Google `UNAVAILABLE/NOT_YET_ELIGIBLE`; YouTube `FAILED/SOURCE_RESULT_MISSING` (`WORKER_RECOVERY · TERMINAL_RESULT_MISSING`). |
| `2332bfaf8f450310b0a4e1aa664a6991` | 03:32–03:35 | `PARTIAL`; Wikimedia `SUCCESS/CACHED`, 1,000 records and 0 provider requests; YouTube `PARTIAL/STALE_CACHE`, 47 records and 0 provider requests (3 provider records omitted by the storage limit); Bing `UNAVAILABLE/NO_SAVED_CANDIDATE`; Google `UNAVAILABLE/NOT_YET_ELIGIBLE` with 0 attempts. |

The second run returned 1,047 provider records (1,000 Wikimedia plus 47 cached YouTube). Its snapshot was persisted as `PARTIAL`; collection processing ended `MODEL_UNAVAILABLE/MODEL_INFERENCE_DEFERRED`, so no Collection assignments were produced. The dashboard's 1,309 keyword total is the existing saved ledger after snapshot reconciliation, not a claim that every provider record became a new keyword. No third run was started. The second run reused caches and made zero new provider requests; only the first run's Wikimedia result reports a new provider request.

Live QA exposed a timestamp defect: Run History and the dashboard's “last Fetch” card showed 03:14, the time cached Wikimedia data was first fetched, although the second Fetch ran at 03:32–03:35. Commit `2aa7f026c47c171dfe6696f2d678e940b84d73ed` corrected history ordering to use the job update timestamp, returns a distinct `run_at` in detail responses, and uses snapshot `created_at` for dashboard collection-time/age labels while preserving provider `fetched_at` as source provenance. Regression coverage checks both values and ordering. Git-backed Production deployment `dpl_E4CAyrazkdPi2MCre5YGF9dHycYk` is `READY` on that commit and owns the existing alias. Read-only Production verification confirmed Run History shows 03:35 and 03:16 for the two accepted runs, and the latest-fetch card shows the 03:35 snapshot time. No additional Fetch was sent for timestamp verification.

Vercel runtime logs for the preceding deployment recorded two `POST /api/v1/fetches` invocations timing out at the platform's 60-second limit during the second run (2026-11-22 20:35:37Z and 20:36:34Z). These were same-key continuation requests; the durable job nevertheless reached and persisted its terminal `PARTIAL` result. Vercel's one-hour aggregate contained four timeout events, while those two timestamps were the matching Fetch POST entries inspected for this run. The client had treated an uncertain continuation acknowledgement like an initial submission and could immediately replay the continuation while the first invocation was still executing. The latest local patch disables automatic continuation replay after an uncertain acknowledgement, restores the existing Fetch ID, and polls that job before considering another source slice. A regression test verifies the second request is a status GET on that same ID and that no immediate duplicate POST is made. This client fix is not yet deployed or live-tested; the two-run Production limit has been reached, so verification after deployment is limited to read-only checks. On the current deployment, read-only runtime-error scans returned no new errors after it became ready. No Vercel Drains are configured; the project Observability Plus view is unavailable on the current plan, so monitoring coverage is limited to available runtime logs.

The Firestore migration remained a dry run against snapshot version 4. It passed integrity, foreign-key, schema and identity checks and reported the planned merge counts in `Documents/D6_VERCEL_LOCAL_DATA_IMPORT.md`. No import data was applied; the separate explicit confirmation required for `--apply` has not been given.
