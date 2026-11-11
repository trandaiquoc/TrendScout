# Báo cáo TrendScout Demo D2

**Ngày:** 11/11/2026

**Phạm vi:** redesign UI/UX, Light/Dark, VI/EN, frontend modules, kiểm thử browser offline và Vercel static-asset parity.

**Trạng thái:** `D2_UI_ACCEPTED` · `FETCH_EXECUTION_BLOCKED_PENDING_AUTH_SESSION` · **không** `MULTI_SOURCE_DEMO_ACCEPTED`.

## 1. Baseline và phạm vi

- Branch: `main`.
- Baseline D2: `539bf386e0cc6c54c47891d2b2afa8c26e8cab29`.
- Trước commit báo cáo, implementation HEAD là `3026072b3594ca50949df698e74ad30988663eee` và `origin/main` vẫn ở baseline. D2 có ba commit local phía trên baseline.
- Đã đọc báo cáo D0/D1, tài liệu source capability/query plan, hướng dẫn repository, mã frontend, browser tests, route API và Vercel packaging.
- Không đổi API schema, route hiện có, storage, source identity, metric semantics, algorithm ID, Wikimedia scoring hay Golden Expected.
- Knowns MCP không có project được chọn trong phiên này; không tạo task/doc ở dịch vụ đó. Repository và yêu cầu đính kèm là nguồn triển khai D2.

## 2. UX và 10 màn hình

Dashboard được thiết kế lại theo kiểu workspace phân tích: sidebar gọn, topbar trạng thái, nội dung có phân cấp, cards/table phẳng, responsive và CTA Fetch nổi bật nhưng disabled có giải thích. Navigation giữ đủ 10 màn hình:

1. Overview
2. Sources
3. Runs
4. Explore Keywords
5. Algorithm Comparison
6. Data Quality
7. Refresh Schedule
8. Product Research
9. AI Usage
10. Settings

Search, category filter, sort, API pagination, mở run đã lưu, keyword detail, evidence URL đã kiểm tra, quality flags, trạng thái partial/stale/synthetic và cảnh báo missing-vs-zero đều được giữ. Các màn hình schedule, algorithm comparison và product research vẫn mô tả đúng trạng thái illustrative/unconfigured; không tạo metric hoặc dữ liệu thương mại.

## 3. Theme và locale

- Lần đầu mặc định **Light + Tiếng Việt**; không dùng system/browser locale để đổi mặc định.
- Dark/English là tùy chọn và được lưu riêng bằng `trendscout.theme` và `trendscout.locale`. Giá trị giới hạn lần lượt ở `light/dark` và `vi/en`; preference lỗi hoặc không đọc được thì fallback Light/VI.
- Script đầu trang áp dụng preference trước render để tránh flash. Không lưu token, API key, Run ID hoặc dữ liệu người dùng trong storage.
- Bản dịch VI và EN là hai catalog độc lập; test kiểm tra key trong HTML và key tĩnh trong UI logic. Date/number dùng `Intl`; status/error phát sinh từ request dùng thông điệp theo locale. Tên keyword/entity, source IDs và algorithm IDs giữ nguyên dữ liệu gốc.
- Đã sửa nhãn Run động để đổi theo locale và thêm kiểm tra contrast cho các cặp text/status token quan trọng của cả hai theme (tối thiểu 4.5:1).

## 4. API boundary và Fetch/auth

`Frontend/public/modules/api.js` là API client duy nhất. Nó chỉ phát same-origin `GET`, có timeout giới hạn, `AbortController`, kiểm tra JSON và thông điệp cho 401/403/404/409/429/503, lỗi 5xx khác, timeout và lỗi kết nối. Các view không tự gọi `fetch`.

Overview có CTA “Thu thập dữ liệu mới / Fetch new data”, nhưng nút bị disabled vì UI chưa có authenticated operator session. Nút không gửi request, không hỏi token/Run ID và không giả lập progress. Tra cứu Run ID chỉ có trong màn hình Runs nâng cao và chỉ đọc. Backend `POST /api/runs` cùng cơ chế Bearer auth hiện hữu không đổi; Vercel giữ read-only và từ chối POST. Không bypass auth, không gọi `/api/config` để suy ra capability.

Vì vậy giao diện được nghiệm thu; thực thi Fetch vẫn bị chặn có chủ đích tới khi có thiết kế session/auth được duyệt và xác minh.

## 5. Tình trạng bốn nguồn

- **Wikimedia Pageviews:** adapter đã triển khai; dùng endpoint top-per-country `US/all-access`, metric `views_ceil` xấp xỉ; kết nối live chưa được xác minh. Phạm vi country endpoint không chứng minh vị trí từng người đọc hay nhu cầu thị trường.
- **YouTube:** chưa có adapter hoặc quyền truy cập đã xác nhận; không được gọi.
- **Google Search:** chưa có adapter/provider trung gian/quyền sử dụng đã xác nhận; không được gọi.
- **Bing Search:** chưa có adapter hoặc quyền truy cập đã xác nhận; không được gọi.

UI không gộp các nguồn và nói rõ vắng mặt không phải số 0, top-list có thể bị kiểm duyệt, lượt xem xấp xỉ, và ngôn ngữ/region parameter không xác lập nhu cầu US. `MULTI_SOURCE_DEMO_ACCEPTED` không được ghi nhận.

## 6. Module, static serving và backend compatibility

Frontend hiện có `api.js`, `format.js`, `i18n.js`, `i18n/vi.js`, `i18n/en.js`, `theme.js`; `app.js` điều phối màn hình và tương tác. HTML/CSS thuần, không thêm framework, dependency frontend hay bundler.

FastAPI phục vụ static path lồng nhau dưới `/dashboard/` sau cùng cơ chế auth hiện hữu. Kiểm tra traversal và giới hạn asset vào `Frontend/public`; Vercel build đóng gói đệ quy mọi file không ẩn thành `dashboard_assets.py`. Không chuyển dashboard sang CDN/public path. Route API, auth Basic cho read-only Vercel, no-store behavior và POST 405 được giữ.

Local test chứng minh module được phục vụ và bundle bằng byte parity với **9 assets**. Chưa chạy build hoặc deployment trên Vercel thật.

## 7. Kiểm thử và kết quả

Lệnh regression cuối:

```bash
bash Backend/scripts/verify_offline_readiness.sh
```

Kết quả `OFFLINE_READINESS_VERIFICATION=PASS`:

- Backend unittest: **398/398 PASS**.
- Golden Regression: **7/7 PASS** với `PYTHONHASHSEED=1` và **7/7 PASS** với `PYTHONHASHSEED=983`; Golden Expected không đổi.
- `pip check`, Bash syntax, `node --check` cho mọi JS asset: PASS.
- Vercel bundle/source byte parity: **9/9 assets PASS**.
- Chrome offline workspace/API/XSS: **768, 1024, 1280, 1440 px PASS**, gồm 10 view, theme/locale persistence, errors và chỉ GET.
- Chrome keyboard/mobile: **320 và 375 px PASS**, tất cả view không overflow; Tab/Shift+Tab, Enter, theme control, Escape dialog, đóng mobile nav và focus restoration được kiểm tra.
- Pipeline synthetic adapter → API → Chrome: PASS.
- Workload fake storage/API 1k, 5k, 10k entity: PASS.
- Ruff check và format check các file Python đã thay đổi: PASS.
- Screenshot runner offline: PASS; Runtime exception và `console.error` không ghi nhận.

Browser tests bao gồm synthetic/partial/stale, search/filter/pagination/detail, XSS fixture, 401/403/404/409/429/500/503, invalid JSON, timeout, network failure, polling có giới hạn, disabled Fetch và không có credential header/storage. Các fixture này là dữ liệu kiểm thử tổng hợp, không phải kết quả live.

## 8. Screenshot evidence và accessibility

Các ảnh WebP compact được tạo offline và xem trực tiếp:

- [Desktop Light · VI · Overview](evidence/d2/desktop-light-vi-overview.webp)
- [Desktop Dark · EN · Overview](evidence/d2/desktop-dark-en-overview.webp)
- [Mobile Light · VI · Sources](evidence/d2/mobile-light-vi-sources.webp)
- [Mobile Dark · EN · Overview](evidence/d2/mobile-dark-en-overview.webp)
- [Desktop Dark · EN · Keyword detail](evidence/d2/desktop-dark-en-keyword-detail.webp)

HTML dùng landmark/heading/labels, skip link và live region; focus-visible, native modal dialog, Escape, focus return, reduced-motion, mobile nav `aria-expanded`, nav `aria-current`, và button state được kiểm tra. Màu trạng thái không phải dấu hiệu duy nhất; có text label. Automated token check bao phủ các cặp text/status chính đạt 4.5:1.

Đây chưa phải chứng nhận WCAG 2.2 AA toàn diện: chưa chạy screen reader/assistive technology hoặc kiểm toán độc lập toàn bộ trang/interaction.

## 9. Self-review bốn góc nhìn

- **Code quality:** module ownership/API boundary rõ; kiểm tra error, null-vs-zero, HTML text rendering, locale fallback và polling bound. Review phát hiện contrast thấp ở secondary/amber tokens và nhãn Run chưa localized; đã sửa, thêm regression checks, chạy lại suite.
- **Architecture:** modules ES được phục vụ và bundle cùng auth path; không thêm framework hoặc đổi endpoint.
- **Security:** không có `innerHTML`, frontend credential, auth header hay write flow; evidence URL chỉ cho HTTPS Wikimedia-family host; path traversal bị từ chối. Secret scan chỉ gặp Bearer literal tổng hợp dành riêng cho test pipeline (`synthetic-browser-test-token-only`), không phải credential.
- **Completeness:** đủ 10 view, 4 trạng thái nguồn trung thực, theme/locale persistence, responsive, keyboard/browser flows, Vercel parity và regression. Không còn finding P1/P2 mở.

## 10. Files changed

- **Frontend:** `Frontend/README.md`; `Frontend/public/index.html`, `app.js`, `styles.css`; `Frontend/public/modules/{api.js,format.js,i18n.js,theme.js,i18n/{vi.js,en.js}}`.
- **Backend integration/docs:** `Backend/app/main.py`, `Backend/scripts/prepare_vercel.py`, `Backend/scripts/verify_offline_readiness.sh`, `Backend/README.md`.
- **Tests:** `Backend/tests/browser_d2_visual_offline.py`, `browser_keyboard_offline.py`, `browser_pipeline_e2e_offline.py`, `browser_workspace_offline.py`, `test_d2_dashboard_contract.py`, `test_frontend.py`, `test_vercel_readonly.py`, `test_workspace_ui.py`.
- **Evidence/report:** năm ảnh WebP trong `Documents/Reports/evidence/d2/` và báo cáo này.

## 11. Blockers, giới hạn và rollback

- Fetch cần authenticated operator session; chưa có session contract ở frontend và không được dùng token thủ công trong UI.
- Live Wikimedia chưa được thử; YouTube/Google Search/Bing chưa cấu hình. Không có live-provider, Firestore, emulator, Vercel build/deploy hoặc paid API call trong D2.
- Known MCP trả về trạng thái không có project được chọn; công việc và review vẫn được thực hiện trực tiếp trong repository.
- Rollback an toàn bằng revert, không rewrite history: `git revert --no-edit <D2-report-SHA> 3026072b3594ca50949df698e74ad30988663eee ad2ab5ea47f0aaf015090805c2e0c6cf740a9598 73edcd59fc91240156ba7097b251a1997ac657c6` (revert theo thứ tự docs → tests → backend assets → UI). SHA của report commit có trong `git log` sau commit cuối.

## 12. Acceptance verdict và commits local

| Tiêu chí | Kết quả |
|---|---|
| Light/VI mặc định, Dark/EN toggle và lưu preference | PASS |
| 320–1440 responsive, 10 màn hình, keyboard/dialog | PASS |
| Fetch không cần input, disabled khi thiếu session, fail closed | PASS — thực thi bị chặn có chủ đích |
| API client riêng, GET/read-only, lỗi có giới hạn | PASS |
| Vercel module serving, auth path, bundle parity | PASS offline; chưa xác minh build live |
| Bốn source status trung thực; không gộp đa nguồn | PASS |
| Keyword lookup/detail/filter/search/pagination và semantics | PASS |
| Backend regression + Golden + browser E2E | PASS |
| Deployment/live provider/Firestore | Không thuộc D2; không chạy |

Local commits, chưa push:

- `73edcd59fc91240156ba7097b251a1997ac657c6` — `feat(demo): redesign dashboard with themes and i18n`
- `ad2ab5ea47f0aaf015090805c2e0c6cf740a9598` — `refactor(frontend): serve browser modules safely`
- `3026072b3594ca50949df698e74ad30988663eee` — `test(demo): cover D2 UX and browser workflows`
- Báo cáo này sẽ nằm trong commit tài liệu cuối riêng; SHA commit đó được nêu ở handoff cuối và `git log`.

**Xác nhận:** không chạy `git push`, không deploy, không thay đổi cloud resource.

## 13. Đề xuất phạm vi D3

Chỉ bắt đầu D3 khi được yêu cầu: thiết kế và review authenticated operator session/capability contract, sau đó bật Fetch có xác nhận quyền, idempotency, trạng thái lỗi và audit phù hợp. Không tự mở D3 trong thay đổi này; các provider ngoài Wikimedia cần capability/terms riêng trước khi triển khai.
