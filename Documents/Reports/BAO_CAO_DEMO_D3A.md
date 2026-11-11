# Báo cáo TrendScout D3A — Public Fetch ngoại tuyến

**Ngày:** 11/11/2026

**Branch:** `main`

**Baseline:** `aecc5671d2c463597b038413f4810c2e33ced7aa`

**Trạng thái tổng:** `PARTIAL_D3A`

## 1. Kết luận

- `PUBLIC_FETCH_FOUNDATION_OFFLINE_VERIFIED`: API, admission policy, fake adapter/storage và browser one-click workflow được kiểm chứng trong môi trường local/test ngoại tuyến.
- `LIVE_FETCH_DISABLED_PENDING_APPROVAL`: public live Fetch chưa được bật. Chưa có distributed limiter, durable idempotency/quota ledger hoặc biện pháp chống lạm dụng đa host đủ để mở endpoint live.
- `MULTI_SOURCE_DEMO_NOT_ACCEPTED`: chỉ có một fixture synthetic gắn với source ID Wikimedia; không có bằng chứng gọi Wikimedia live hay tích hợp YouTube, Google Search, Bing Search.
- `PARTIAL_D3A`: nền tảng offline hoàn thành; bảo đảm chống lạm dụng cho public live còn thiếu. Không coi đây là nghiệm thu production.

## 2. Thay đổi kiến trúc

Luồng D3A trước đó `Authenticated Operator Session` được thay bằng `Backend-Controlled Public Fetch`:

1. Trang web đọc capability công khai, không yêu cầu tài khoản.
2. Nút Fetch chỉ bật nếu backend cho phép runtime fixture tổng hợp.
3. Frontend gửi `POST /api/v1/fetches` với `{}` và idempotency key tạm trong bộ nhớ.
4. Backend chọn source, query plan, mode và budget; client không truyền URL, query, provider, Run ID, plan ID, quota hoặc permission.
5. Frontend poll status có giới hạn và tải kết quả synthetic khi run kết thúc.

Không có login/logout/register/user-account flow trong dashboard. `POST /api/runs` legacy không bị thay đổi: vẫn yêu cầu Bearer operator token. Vercel vẫn read-only; các POST fetch/run trả 405.

Vercel HTTP Basic gate cho dashboard read-only là cấu hình hosting có từ trước và được giữ nguyên theo phạm vi không sửa D0/D1/D2; URL Vercel hiện tại không được nghiệm thu là mở không cần password. Nếu yêu cầu “website công khai, không password” bao gồm cả hosting gate thì phần đó còn chờ quyết định/cập nhật riêng, không được che giấu như đã hoàn tất.

## 3. API và chính sách admission

| Hạng mục | Hành vi được triển khai |
| --- | --- |
| `GET /api/v1/capabilities` | Anonymous, `Cache-Control: no-store`, không trả credential; mô tả bốn nguồn, trạng thái adapter/approval/config/live verification, plan và giới hạn. |
| `POST /api/v1/fetches` | Endpoint riêng; chỉ nhận JSON `{}`; body tối đa 1 KiB; cần idempotency key 16–128 ký tự; trả 202 hoặc lỗi admission. |
| `GET /api/v1/fetches/{fetch_id}` | ID ngẫu nhiên opaque; đọc trạng thái, không khởi động công việc; tối đa 120 lượt poll trong TTL một giờ. |
| Feature flag | `TREND_SCOUT_PUBLIC_FETCH_ENABLED`, mặc định `false`; chỉ profile local/test + mode synthetic + fake storage + adapter fixture rõ ràng. |
| Source/query | Allowlist cố định một source ID; query plan `demo-us-en` do server chọn; tối đa 1 request và 1 page cho mỗi source. Không gọi provider thật. |
| Admission | Tối đa 3 lượt mới trong 60 phút, một run đồng thời, cooldown hiện hữu, deadline mặc định 60 giây (cấu hình server-side 1–300 giây). |
| Retry/idempotency | POST không tự retry ở browser; cùng key/fingerprint trong process trả cùng fetch; concurrent duplicate tạo tối đa một run. |
| Read-only/multi-process | Vercel/read-only từ chối POST. Một khóa file trên cùng filesystem chỉ cho một app process chạy; process thứ hai fail closed. |

Các bộ đếm và idempotency hiện lưu trong memory và reset khi restart. Khóa file chỉ có ý nghĩa trên filesystem dùng chung của cùng máy. Đây không phải distributed guarantee.

## 4. Đánh giá lạm dụng và bảo mật

- **HTTP client trực tiếp/bot:** có thể gọi endpoint nếu môi trường đã bật flag; không có user authentication. Request vẫn bị ràng buộc bởi body rỗng, source/plan/budget server-side và giới hạn admission. Client có thể tiêu thụ quota synthetic hữu hạn.
- **Nhiều IP:** giới hạn hiện là global trong process, không dựa vào IP nên xoay IP không tăng budget của cùng process. Không có quota bền vững giữa process/host hoặc sau restart.
- **Nhiều process:** process thứ hai trên cùng filesystem fail closed qua `flock`; cấu hình worker nhiều hơn một cũng làm policy từ chối. Host/container riêng với filesystem riêng không chia sẻ khóa hoặc counter, vì thế mô hình này không đủ cho public live.
- **Origin/Host:** kiểm tra Host local/test, Origin khớp origin request nếu có, Fetch Metadata và từ chối forwarding headers không tin cậy. Đây là tín hiệu chống lạm dụng trình duyệt, không phải xác thực; direct clients có thể tự tạo header.
- **Chi phí/provider:** profile production, live mode, read-only hoặc cấu hình cloud không được bật Public Fetch. Endpoint cần dependency được đánh dấu offline-only và không gọi Wikimedia, YouTube, Google, Bing hay Firestore.
- **Duplicate/retry storm:** key được kiểm tra server-side và chốt trong critical section; browser không tự retry POST, retry thủ công giữ cùng key khi kết quả chưa rõ; polling và body có giới hạn.
- **Các việc cần trước public live:** phê duyệt riêng; distributed limiter/concurrency; durable idempotency và quota/cost ledger; bảo vệ bot/IP rotation; provider-specific caps, monitoring/alerting và rà soát điều khoản nguồn.

Vì các bảo đảm phân tán còn thiếu, live Fetch vẫn disabled. Không dùng Origin check, nút disabled hoặc bộ đếm in-memory làm căn cứ mở live.

## 5. Frontend và phạm vi D2 được giữ

Giữ nguyên mười màn hình, Light/Dark, VI/EN, responsive layout, keyword search/filter/detail và source status. Fetch logic tách trong `modules/fetch.js`; capabilities tách trong `modules/capabilities.js`. Khi capability lỗi hoặc backend từ chối, nút disabled và lý do được hiển thị theo locale. Khi fixture chạy, UI báo trạng thái backend thật, partial/completed/error và đọc run/result; không tự tạo phần trăm tiến độ.

Không gửi credential/token từ browser và không lưu idempotency key/fetch ID vào localStorage/sessionStorage. Source capability không được diễn giải thành live connectivity. Wikimedia live chưa xác minh; ba nguồn còn lại chưa được duyệt/cấu hình.

## 6. Kiểm thử và xác minh

Kết quả dưới đây được ghi sau khi hoàn tất cổng offline:

- `bash Backend/scripts/verify_offline_readiness.sh`: **PASS**, toàn bộ 11 gates.
- Backend full suite: **412 tests PASS**.
- Golden Regression: **7/7 PASS** với `PYTHONHASHSEED=1` và **7/7 PASS** với `PYTHONHASHSEED=983`.
- Vercel asset byte parity: **11/11 assets PASS**; test xác nhận `POST /api/v1/fetches` vẫn 405 ở read-only.
- Chrome one-click anonymous synthetic E2E: **PASS**; duplicate click tạo một POST, trạng thái fixture và kết quả được hiển thị.
- Chrome workspace: **PASS** ở 768, 1024, 1280, 1440 px; keyboard/mobile 320/375 px: **PASS**.
- Pipeline adapter → API → Chrome synthetic E2E: **PASS**; benchmark fake storage 1k/5k/10k: **PASS**.
- Ruff scoped, JavaScript syntax và `git diff --check`: **PASS**.
- Không chạy provider live, Firestore/Emulator, deploy hoặc push.

Targeted Public Fetch tests bao gồm anonymous capabilities, default-off, body/source/origin validation, idempotency concurrent duplicate, rate limit/cooldown, second process fail closed, status polling, credential non-disclosure và legacy Bearer protection.

## 7. Files thay đổi

- Backend: `app/api/capabilities.py`, `app/api/fetches.py`, `app/orchestration/public_fetch.py`, `app/main.py`, `app/settings.py`, `app/orchestration/runs.py`, `app/sources/catalog.py`, `app/storage/fake.py`.
- Frontend: `public/modules/api.js`, `public/modules/capabilities.js`, `public/modules/fetch.js`, `public/app.js`, `public/index.html`, `public/styles.css`, VI/EN catalogs.
- Verification: Public Fetch API tests, browser E2E, settings/source/Vercel/D2 contract tests, workspace offline test, offline readiness script.
- Docs: `Backend/README.md`, `Frontend/README.md`, `Documents/Design/DEMO_PUBLIC_FETCH_AND_ADMISSION.md` và báo cáo này.

Không sửa D0/D1/D2 reports, Golden Expected hoặc công thức Wikimedia scoring.

## 8. Rollback và commits

Không có schema migration hay dữ liệu cloud. Rollback bằng `git revert` các commit D3A theo thứ tự ngược từ mới nhất; không reset/rebase và không sửa lịch sử trước baseline.

- Commits local cho implementation/tài liệu nền tảng: `09e60b241a0830744645477cb70fcf8309eadc5a`, `8b3817d18b983c14571e18b533e919dcfb64181b`, `23009e5eedb629fffb1484b74d568963e1ac3565`.
- Báo cáo này được ghi trong một commit local riêng sau ba commit trên.
- Git push: **không thực hiện**.
- Deploy: **không thực hiện**.

## 9. Bước tiếp theo

Chỉ bắt đầu D3B sau chỉ dẫn riêng. Trước khi đề xuất bật live Fetch, cần hoàn thành và review các kiểm soát phân tán/chi phí ở mục 4; multi-source demo chưa được nghiệm thu.
