# Giai đoạn 2 — Bước 20: Dashboard và tenant safety preview

**Kết luận: `OFFLINE_ACCEPTED — UX_AND_TENANT_POLICY_PENDING_APPROVAL`; `BLOCKED_FOR_LIVE`.** Đây là workspace demo và authz giả trong một process; không có multi-tenant auth/RBAC, AI settings, scheduler, BYOK hay research persistence production. Không gọi Wikimedia, Firebase, AI hoặc marketplace; không deploy, bật billing hoặc thay IAM.

## Audit capability và dashboard

Trước thay đổi, dashboard chỉ có Overview, load run và bảng keyword; API hiện có health, config read-only, create manual run có operator auth, GET run/list/detail keyword. Không có GET all runs, scheduler, research, settings/usage, tenant membership hoặc secret API an toàn. Điều hướng mới có mười màn hình, breadcrumbs và active state; layout desktop/sidebar và thanh điều hướng cuộn trên màn hình nhỏ. Input Run ID vẫn ở thanh chung, không tự fetch khi mở trang. Không tự đổi scoring, classification hoặc dữ liệu.

| Capability | Trạng thái | Thực tế UI |
|---|---|---|
| Overview, Runs, Keywords, Detail | `REAL_API_AVAILABLE` | Dùng API demo hiện hữu; một Run ID do người dùng nhập, search/filter/sort/pagination/detail; không giả vờ có danh sách toàn bộ runs. Manual POST giữ auth cũ, config read-only lỗi thì fail-closed. |
| Sources, Quality | `REAL_API_AVAILABLE` một phần | Mô tả adapter Wikimedia hiện có; coverage/status/fetched time lấy từ run khi có. Cleaning/rejection totals chưa có API → `NOT_MEASURED`; nguồn tương lai `NOT_CONFIGURED`. |
| Algorithm Comparison | `OFFLINE_PREVIEW_ONLY` | Fixture synthetic cố định có chọn algorithm, scope/metric/window/dataset minh họa, `COMPLETED`/`SKIPPED`/`FAILED`; `NOT_COMPARABLE`, không xếp chung hoặc tuyên bố winner. |
| Product Research | `OFFLINE_PREVIEW_ONLY` | Nhãn `OFFLINE DEMO / SYNTHETIC DATA`; `UNKNOWN`, opportunity thiếu evidence, idea `UNVERIFIED`, `APPROVED_FOR_RESEARCH` chỉ là quyền nghiên cứu tiếp. Không có dữ liệu tenant thật hoặc marketplace link. |
| Weekly Refresh, source enable | `DISABLED_PENDING_BACKEND` / `DISABLED_PENDING_APPROVAL` | Start/Pause/Stop và enable nguồn chưa triển khai bị disabled; không cron, scheduler hoặc polling tự động cho chức năng này. |
| AI Usage / Settings | `DISABLED_PENDING_BACKEND` / `DISABLED_PENDING_APPROVAL` | `DISABLED`, provider/model `UNCONFIGURED`, usage `NOT_MEASURED`, `COST_UNKNOWN`, `SERVER_SIDE_REQUIRED`. BYOK và Test Connection disabled. Checkbox chỉ là draft synthetic trong bộ nhớ trang, reset được, không lưu server/browser storage. |
| Tenant/RBAC/secret | `OFFLINE_PREVIEW_ONLY` / `DISABLED_PENDING_APPROVAL` | Contract fake độc lập; không thêm middleware hoặc public API và không thay Basic/read-only hay operator auth hiện tại. |

Detail hiển thị baseline, missing dates, score components, source/project/scope, category và `NOT_AVAILABLE`/`NOT_ASSESSED` cho field cũ không có. Score null giữ `—`/chưa đủ dữ liệu; missing, 404, partial, stale được báo rõ; thời điểm fetch lấy từ run, không dùng giờ tải trang. Không biến signal thành demand. Không có raw provider API key input; operator token của manual run cũ vẫn nhập trong dialog, chỉ giữ tạm trong bộ nhớ và được xóa sau submit/đóng.

## Security và state

`Backend/app/security_preview/offline.py` tách fake authentication identity/membership khỏi authorization theo role, action và resource owner. Viewer, analyst, operator, reviewer, tenant admin và platform admin là policy minh họa **chưa được duyệt**; shared dataset chỉ cấp dataset read, không cấp research, budget, settings hay audit tenant khác. Fake token có thể được expire/revoke; denied action có audit tối thiểu với actor hash. Trusted registration là ranh giới test, không phải tenant membership server thật. Secret metadata chỉ chấp nhận reference giả, provider/status allowlist, nhãn masked, tenant ID và timestamp hợp lệ; không có trường secret value. Không triển khai resolver, vault hay BYOK.

Browser render untrusted content bằng `textContent`/DOM APIs; source evidence URL chỉ cho HTTPS Wikimedia-family hostname, không credential, `noopener noreferrer`; javascript/data, hostname giả, relative và encoded payload bị từ chối. Không có `innerHTML`, localStorage hoặc sessionStorage trong frontend. API client có timeout kể cả đọc JSON; request sequence ngăn response run/search cũ ghi đè mới, detail invalidated khi đổi run/đóng dialog; search debounce và không tạo thêm polling scheduler. `POST /api/runs` bị chặn trong UI khi read-only/config lỗi; backend read-only vẫn enforce HTTP 405 độc lập.

## Browser, accessibility và failure matrix

Kiểm thử Chrome thật bằng hai script standalone trong `Backend/tests/`, API transport synthetic được tiêm trong test; không server hay dữ liệu cloud. `browser_workspace_offline.py` kiểm tra DOM ở viewport thực 500, 768, 1280 px (Chrome giới hạn cửa sổ được yêu cầu 400 thành 500), cùng read-only và config lỗi. `browser_keyboard_offline.py` dùng DevTools pipe mô phỏng viewport thật **320 và 400 CSS px**, kiểm tra không overflow từng màn hình và gửi phím Tab, Enter, Escape thật; dialog focus/close, labels, headers và aria-live cũng được kiểm tra. Có xem screenshot synthetic tạm dưới `/tmp`, không commit artifact. Chưa kiểm định screen reader thực, mọi tổ hợp thiết bị, contrast đầy đủ hoặc chứng nhận WCAG 2.2 AA.

| Failure matrix | Bằng chứng |
|---|---|
| 1–5 desktop/mobile/tablet, keyboard, focus/dialog | `browser_workspace_offline.py`, `browser_keyboard_offline.py`; 320/400/500/768/1280 px, Tab/Enter/Escape. |
| 6–13 API cũ, search/filter/page, race, fail/empty/null/partial/stale | Browser offline fake + `test_golden_contract.py`, `test_runs.py`, `test_frontend.py`; run A/B và search response cũ kiểm tra riêng. |
| 14–19 comparison, quality, source và refresh disabled | Browser test kiểm tra `SKIPPED`/`FAILED`, `NOT_COMPARABLE`, source unconfigured, 404 và disabled controls; không POST. |
| 20–23 AI disabled/unconfigured, test connection disabled, không key storage | Browser config/read-only/error checks, draft reset, storage rỗng; `test_secret_contract_rejects_raw_values_and_timestamp_smuggling`. |
| 24–29 fake tenant/budget/roles/reviewer/shared isolation | `test_workspace_security.py`: role matrix, authorized role cross-tenant, expired fake identity và private review/audit. Không chứng minh auth production. |
| 30–36 research null/synthetic/unverified và XSS/URL | Browser synthetic view + text payload `<img ...>`, URL xấu, no injected DOM; research preview không có giá/sales. |
| 37–40 Vercel 405, auth sai/expired, API error và không live calls | `test_vercel_readonly.py`, fake expired identity, browser 404/config 503, seccomp. Basic demo không có session expiry production; cần thiết kế riêng. |

## Regression, mạng và các giới hạn

**357/357 unittest pass**, không có test skip; Golden **7/7** cho mỗi seed 1 và 983. `pip check`, `bash -n setup.sh start.sh` và `node --check Frontend/public/app.js` pass. Tất cả unittest chạy qua seccomp chặn socket/connect/io_uring và đóng fd kế thừa. Chrome cần AF_UNIX để giao tiếp nội bộ; launcher riêng chỉ cho AF_UNIX, chặn các họ socket khác và io_uring, bỏ proxy env, tắt background networking; Python/native AF_INET, AF_INET6 và child bị EPERM. Các browser tests kiểm tra blocker trước khi khởi động Chrome, API được fake trong trang. Không có kết luận về live cloud hay production isolation từ test này.

Script build Vercel tạo bundle tạm được đối chiếu bytes với ba tệp frontend rồi xóa artifact; không deploy. Public API, schema Firestore, scoring/classifier, scheduler/worker, AI/research và retention runtime không đổi; Vercel read-only test giữ POST 405. Screenshots/log/browser profiles chỉ nằm ở `/tmp`, không commit.

PENDING_APPROVAL: role/membership thật, tenant RBAC, secret management/BYOK, provider/model/data sharing/budget, scheduler/hosting/retention và product research policy. Trước live cần backend authz/RBAC và session expiry thật, kiểm thử tenant và storage bền vững, IAM, secret rotation, CSRF/rate-limit, accessibility/contrast và live tests được phép riêng. Rollback bằng bỏ các thay đổi frontend và không nối fake security service vào runtime. **`BLOCKED_FOR_LIVE` giữ nguyên.**
