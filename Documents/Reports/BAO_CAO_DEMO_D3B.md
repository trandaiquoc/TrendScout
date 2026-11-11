# TrendScout — Báo cáo D3B: YouTube Data API v3 adapter

**Ngày kiểm tra:** 11/11/2026 (ICT) · **Branch:** `main` · **Baseline D3A:** `08ab1df9ef2b1a624c9c93f37f2cb4f9f238e78d`
**Kết quả:** `D3B_YOUTUBE_ADAPTER_OFFLINE_VERIFIED`

## Trạng thái nghiệm thu

- `D3B_YOUTUBE_ADAPTER_OFFLINE_VERIFIED`: adapter query-based, hợp đồng dữ liệu, pagination, budget, lỗi, provenance và regression chạy với fixtures/fake dependencies/MockTransport.
- `YOUTUBE_LIVE_NOT_APPROVED`: chưa có project/key được cấu hình hoặc phê duyệt; không có live transport hay live factory.
- `LIVE_FETCH_DISABLED_PENDING_APPROVAL`: YouTube không được thêm vào production source registry hoặc Public Fetch.
- `MULTI_SOURCE_DEMO_NOT_ACCEPTED`: không có YouTube storage, scoring, hay kết hợp metric đa nguồn.

Kết quả PASS chỉ áp dụng cho implementation ngoại tuyến. Không có provider endpoint nào được gọi và fixture không phải bằng chứng live.

## 1. Git baseline và thay đổi trong workspace

Trước triển khai, branch là `main`, `HEAD` ở D3A baseline nêu trên và working tree sạch. Trong lần tiếp tục công việc này, reflog có một entry `reset: moving to HEAD` lúc `2026-11-11 14:55:31 +0700`. Tôi không chạy reset/rebase trong lần tiếp tục này. Adapter và test D3B còn trong Git unreachable tree `672f32db216a8a1605b3be6464e00ed13b737a44`; các file đó đã được khôi phục trước khi hoàn tất và commit. Không có commit D0–D3A nào bị amend, xóa hoặc viết lại.

## 2. Kiến trúc và ranh giới

`CollectionSource`/`PageObservation` vẫn giữ cấu trúc Wikimedia. YouTube là query source riêng, không được ép vào `views_ceil`, Wikimedia cleaning/scoring, legacy run schema hoặc storage.

- `Backend/app/sources/youtube/adapter.py`: chạy batch theo plan, cursor, deadline, cancellation, budget, dedup và partial state.
- `client.py`: fixed `search.list` endpoint, tham số allowlist, header credential, response validation và error classification.
- `models.py`: query execution plan, typed result/evidence, request budget và `record_schema_version=1`.
- `normalizer.py`: validate video resource, ID, text, timestamp và thumbnail URL; tạo canonical URL từ ID đã kiểm tra.
- `pagination.py`: fixed-plan validation, fingerprint, query context, cursor scope và request-budget tracker.
- `errors.py`: safe error code/message, HTTP status, retryability, attempt count, query scope và stop reason.

Không có Google/Bing adapter. YouTube không được đăng ký làm live source. `PUBLIC_SOURCE_IDS` của D3A vẫn chỉ có Wikimedia; Public Fetch không thể chọn YouTube. D2 frontend không đổi; chỉ fixture capability trong browser E2E được cập nhật để phản ánh trạng thái offline mới.

## 3. Request contract và credential boundary

Endpoint cố định `GET https://www.googleapis.com/youtube/v3/search`, lấy từ central source catalog. Request allowlist:

```text
part=snippet
type=video
q=<canonical seed từ demo-us-en v1>
regionCode=US
relevanceLanguage=en
publishedAfter=<UTC RFC3339 cutoff chung của batch>
maxResults=50
order=relevance
pageToken=<provider token đã bind với query context, nếu còn page budget>
X-Goog-Api-Key: [redacted]
```

Đây là request shape được kiểm tra bằng MockTransport; không phải request live đã chạy. Browser không truyền query, URL, provider, API key, run ID hay plan ID. API key chỉ được resolve qua `CredentialResolver` phía server và đặt trong `X-Goog-Api-Key`; không nằm trong URL, result, safe error hoặc log. Hiện không có resolver đọc environment, secret thật hay app composition cho YouTube. Test chỉ dùng `offline-test-key-never-real`.

Tài liệu Google về API key khuyến nghị tránh đưa key vào URL: [API key best practices](https://docs.cloud.google.com/docs/authentication/api-keys-best-practices), [API key use](https://docs.cloud.google.com/docs/authentication/api-keys-use). Tài liệu được đối chiếu ngày 11/11/2026.

## 4. Query plan và pagination

Adapter chỉ nhận plan đăng ký `demo-us-en` v1. Plan giữ nguyên 14 seeds, thứ tự, taxonomy, identity, fingerprint `57f80bbd40ba13bed596f8f7e5fb30787f9cf5ea854cef32d9a4d5cf5713f5ac`, US/en-US scope và default `max_pages_per_query=1`. Test xác nhận plan bị sửa sẽ bị từ chối.

Adapter chụp UTC timestamp một lần khi bắt đầu batch, rồi áp dụng cùng rolling seven-day `publishedAfter` cho mọi query và page. Fingerprint từng query giữ canonical query, scope, plan fingerprint và cutoff.

Pagination dùng `nextPageToken` provider, không dùng offset. Token bind với plan/version, query/fingerprint, source/provider, region, language và cutoff. Adapter phát hiện token sai, token lặp, token dùng nhầm query/cutoff, page cap, budget, deadline, quota/rate limit và cancellation. Default là một page/query; nếu provider trả cursor ở page cap, kết quả báo `MORE_AVAILABLE` / `PAGE_CAP_REACHED`, `complete=false`. Test-only limits cho phép chạy tối đa 5 trang.

## 5. Request budget, quota và lỗi

Budget tracker báo `planned_calls`, `max_requests`, `attempted_calls`, `completed_calls`, `failed_calls`, `retried_calls`, `pages_fetched`, `remaining_budget` và `stop_reason`. Default batch là 14 first-page calls. Mỗi retry reserve thêm một request trước khi gửi; hết budget thì dừng. Giới hạn nội bộ: 14 query, 1–5 page/query, tối đa 500 request, 50 results/page, deadline 0.1–300 giây và response body 1 MB. Shared `HttpPolicy` giới hạn retry count/backoff; default là tối đa 2 retries và 30 giây retry wait.

Google quota guide hiện tài liệu hóa bucket riêng cho `search.list`: mặc định 100 calls/ngày, một quota unit mỗi call; mỗi page tiếp theo tính một call mới. Đây là tài liệu provider, không phải quota đã xác minh cho project TrendScout. Không báo quota provider còn lại từ budget mô phỏng. Tham khảo [YouTube Data API quota](https://developers.google.com/youtube/data/determine_quota_cost), truy cập 11/11/2026.

Error codes gồm `INVALID_CONFIGURATION`, `CREDENTIAL_NOT_CONFIGURED`, `UNAUTHORIZED`, `PERMISSION_DENIED`, `INVALID_QUERY`, `INVALID_PAGE_TOKEN`, `QUOTA_EXHAUSTED`, `RATE_LIMITED`, `TRANSIENT_UPSTREAM_ERROR`, `UPSTREAM_TIMEOUT`, `NETWORK_ERROR`, `MALFORMED_RESPONSE`, `RESPONSE_TOO_LARGE`, `REQUEST_BUDGET_EXHAUSTED`, `RETRY_BUDGET_EXHAUSTED`, `DEADLINE_EXCEEDED`, `CANCELLED` và `PARTIAL_BATCH`. Retry chỉ áp dụng với timeout/network/429/5xx có budget; `Retry-After` bị chặn theo policy. Quota exhaustion dừng phần còn lại của batch; HTTP 403 quota không bị đổi thành lỗi xác thực.

## 6. Normalization, provenance, dedup và metric

Typed record version 1 giữ source/provider, synthetic mode, video ID/canonical URL, title/description/channel metadata, `published_at`, `fetched_at`, plan/version/query fingerprint, filter market/language, rank/page, provider response status, metric descriptor và quality flags. Optional metadata thiếu được giữ là `None` cùng quality flag; ID/timestamp/text không hợp lệ bị reject kèm reason code. Không lưu raw provider body.

Entity dedup theo `(provider_id, provider_video_id)` trong batch. Mọi query/page evidence vẫn được giữ, gồm canonical query, query fingerprint/scope, plan fingerprint/version, cutoff, fetched time, result rank, page và response status. Test xác nhận một video lặp qua 14 query hoặc nhiều page vẫn giữ đủ evidence.

Metric duy nhất là `result_rank` — thứ tự kết quả trong query response cụ thể. Nó không phải view count, search volume, Trending rank hay product demand. Không gọi `videos.list`, không có `view_count`, YouTube trend score hay cross-source score.

## 7. Provider policy và giới hạn diễn giải

- `regionCode=US` là vùng có thể xem video; không chứng minh tác giả/người xem ở Mỹ.
- `relevanceLanguage=en` ưu tiên độ liên quan, không loại trừ tuyệt đối nội dung ngôn ngữ khác.
- `publishedAfter` lọc thời điểm đăng, không đo thời điểm tăng phổ biến.
- Search result/rank là mẫu do truy vấn và thứ tự provider trả về; `pageInfo.totalResults` là ước lượng, không phải số item đã thu thập.
- 14 query là curated demo seeds, không đại diện toàn US market.
- Chưa có phê duyệt về YouTube Developer Policy, quyền tạo derived metrics, attribution, retention/deletion, hoặc analytics/commercial use. Không ghi YouTube data thật vào storage hay kết hợp với nguồn khác.

Tham khảo [Search: list](https://developers.google.com/youtube/data/reference/rest/v3/search/list), [Developer Policies](https://developers.google.com/youtube/terms/developer-policies), [derived metrics and storage policy](https://developers.google.com/youtube/terms/derived-metrics-policy), truy cập 11/11/2026. Cần policy review và xác minh lại tài liệu trước bất kỳ live activation nào.

## 8. Capability và tương thích hệ thống

YouTube capability hiện trả:

```text
adapter_status=IMPLEMENTED_OFFLINE_VERIFIED
readiness=PENDING_APPROVAL
approval_status=PENDING_APPROVAL
configuration_status=NOT_CONFIGURED
live_verification_status=NOT_VERIFIED
default_enabled=false
supported_data_modes=[SYNTHETIC]
```

D3A Public Fetch vẫn accountless, synthetic-only, mặc định disabled; khi chạy chỉ dùng source allowlist Wikimedia. Legacy `POST /api/runs` vẫn cần Bearer operator token. Vercel read-only, D2 Light/Dark, VI/EN, responsive và 10 màn hình không đổi.

## 9. Kiểm thử và regression

Targeted tests:

- YouTube adapter: **30 PASS** — fixed 14-query params, fake credential/header, no credential leakage, one/two-page, empty page, repeated/invalid/cross-query/cross-cutoff token, page cap, duplicate evidence, budget/retry, bounded `Retry-After`, quota, timeout/cancellation, malformed/oversize response, redirect, non-MockTransport rejection, safe normalization/rejection reason và capability.
- Shared HTTP policy: **5 PASS**.
- Source catalog: **6 PASS** — YouTube offline implementation không được enable/register live.
- D3A Public Fetch: **12 PASS** — public capability, synthetic fetch, rate/cooldown/idempotency, multi-process guard và legacy Bearer endpoint.

`bash Backend/scripts/verify_offline_readiness.sh`: **PASS**.

- Backend suite: **443 tests PASS**.
- Golden Regression: **7/7 PASS** với `PYTHONHASHSEED=1`, và **7/7 PASS** với `PYTHONHASHSEED=983`.
- Vercel dashboard bundle parity: **11/11 assets**.
- Strict seccomp: `AF_INET`, `AF_INET6` và connect bị `EPERM`; `AF_UNIX` chỉ được mở cho Chrome IPC.
- Chrome DOM kiểm tra 768/1024/1280/1440px PASS; keyboard/mobile 320/375px PASS; synthetic pipeline → API → dashboard PASS; anonymous one-click synthetic Fetch E2E PASS.
- Workload synthetic 1k/5k/10k entities PASS.
- Dependency consistency, shell syntax và dashboard JavaScript syntax PASS.

Suite có chạy các Firestore emulator guard tests, nhưng không gọi Firestore hoặc khởi chạy Firestore Emulator. Không gọi YouTube, Google Search, Bing Search hay Wikimedia live.

Golden file SHA-256 `Backend/tests/fixtures/golden_v1/expected.json`: `712ba7dd31c93739064d155ef2aaa00ab5ed06a7bff0e87d7f8aa370ac86ffed`; file không nằm trong diff. Query plan, scoring, Wikimedia adapter, legacy storage/run schema, D0/D1/D2/D3A reports và frontend source assets không đổi.

## 10. Files và local commits

Files triển khai: `Backend/app/sources/youtube/{__init__,adapter,client,errors,models,normalizer,pagination}.py`, hai synthetic fixtures và `Backend/tests/test_youtube_adapter.py`.

Files tích hợp/tài liệu: `Backend/app/sources/catalog.py`, `Backend/app/sources/http_policy.py`, `Backend/tests/test_http_policy.py`, `Backend/tests/test_source_catalog.py`, `Backend/tests/test_public_fetch.py`, `Backend/tests/browser_public_fetch_e2e_offline.py`, `Backend/README.md`, `Documents/Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md` và báo cáo này.

Local commits đã tạo, không push:

- `437bc2a98a3315a0d40229ef4539167f2b12ab72` — `feat(youtube): add offline query search adapter`
- `b0606cd4376a0c0c40bbbff228182dfb9301785d` — `test(youtube): preserve public fetch compatibility`

Commit cuối chứa báo cáo này được ghi trong handoff cuối vì SHA của một commit không thể được ghi vào chính nội dung của nó. Rollback bằng `git revert` theo thứ tự mới nhất trước, không reset/rewrite history.

## 11. Blockers và kết luận

Trước live activation cần phê duyệt use case/policy/retention/attribution, cấp project/API key qua secret manager, xác minh quota project thật, thiết kế distributed rate/concurrency/idempotency và cost controls, monitoring/abuse response, rồi review bảo mật riêng. Các bước này không thuộc D3B.

Không deploy, không git push, không gọi live provider, không thay đổi Firestore/Emulator, không bật Public live Fetch và không bắt đầu D4.
