# TrendScout — Giai đoạn 2, Bước 21: nghiệm thu offline và readiness gates

Ngày kiểm định: 10/11/2026
Repository: `/mnt/Data/Mine/TrendScout`
Branch/baseline: `main`, HEAD ban đầu `0ef1980` (`docs: report phase 2 step 20 dashboard acceptance`), cùng commit dashboard `659e721`; lúc bắt đầu working tree sạch và `origin/main` trùng HEAD.

## Kết luận điều hành

- **MVP offline: `MVP_OFFLINE_ACCEPTED`.** Pipeline dữ liệu synthetic chạy qua Wikimedia adapter với MockTransport, cleaning, extraction, classification v0.1, scoring v0.1, FakeStorage, ASGI API và Chrome dashboard. API/Golden/browser regression pass; bộ kiểm thử có thể chạy lại bằng một lệnh đã lưu trong repo.
- **MVP live: `BLOCKED_FOR_LIVE`.** Không gọi Wikimedia, Firestore, AI, marketplace hoặc Vercel; không kiểm tra IAM/quota/storage cloud. Fake lease, publish, retention, backup và auth không chứng minh transaction, durability hoặc multi-host safety.
- **Commercial: `COMMERCIAL_NOT_READY`.** Chưa có production tenant identity/RBAC, nguồn và quyền thương mại được duyệt, privacy/terms, secret operations, cost approval, hosting approval, monitoring/on-call hay production recovery evidence.
- Không có accidental live call trong lượt Bước 21. Sự cố Wikimedia ngoài ý muốn ở Bước 14 vẫn được ghi nguyên trong báo cáo đó và thuộc ngoại lệ lịch sử do chủ dự án chấp thuận; Bước 21 không mở rộng ngoại lệ.

Nghiệm thu offline không đồng nghĩa với `PRODUCTION_READY`, `MVP_LIVE_READY` hoặc `COMMERCIAL_READY`.

## Phạm vi và nguồn tài liệu

Đã đọc `AGENTS.md`, `Frontend/AGENTS.md`, `Documents/REQUIREMENTS_TECH_BA_DEV.md`, thiết kế tổng thể `Documents/TREND_KEYWORD_DEMO_BUSINESS_SYSTEM_DESIGN.md` và các báo cáo Bước 1–20 trong `Documents/Reports/`. Bản requirement và thiết kế là tài liệu tham chiếu; implementation và tests hiện tại là bằng chứng hành vi.

Hai đường dẫn được chỉ định trong yêu cầu không có trong working tree/Git tree: `Documents/HUONG_DAN_CAC_BUOC_HOAN_THANH_GIAI_DOAN_2.md` và `Documents/HUONG_DAN_CAC_BUOC_HOAN_THANH_DEMO.md`. Tài liệu roadmap bước 21 vì thế chưa thể đối chiếu độc lập. Nội dung Bước 21 được cung cấp trực tiếp trong yêu cầu; thiếu hai tài liệu không làm thay đổi tiêu chí đã nêu và được ghi làm khoảng trống tài liệu.

Không tìm thấy một registry quyết định chuẩn chứa các mã `D1–D12`; không tự điền hoặc xem các câu hỏi đang mở trong báo cáo audit như quyết định đã duyệt. Báo cáo Bước 1 ghi các câu hỏi BA còn cần chủ sản phẩm quyết định. Các quy tắc scoring/classification v0.1, retention demo và quyền nguồn vẫn giữ đúng trạng thái/version trong các báo cáo từng bước.

## Kiến trúc đã kiểm tra

`app.main.create_app`/composition khởi tạo FastAPI, API health/run/keyword, dashboard static assets và dịch vụ manual-run hiện hữu. Run pipeline dùng Wikimedia adapter → validate/canonical observations → cleaning/dedup → extraction/classification → trend scoring → storage port. Firestore Admin adapter sử dụng server credentials khi được cấu hình; offline verification injects FakeStorage và MockTransport.

Algorithm Engine mới, scheduler, job coordinator, refresh worker, strict fake dataset publisher, retention, AI/translation/enrichment và product research là các lớp riêng có tests, nhưng scheduler/worker/publisher/cleanup/AI/research không được nối vào luồng production của API. Trên Vercel, `VERCEL=1` ép read-only; composition không khởi tạo run service/recovery hoặc write worker. Manual run local là hành động tường minh có operator token; thiếu token fail-closed. Dashboard không tự tạo run hoặc gọi provider.

## Acceptance Matrix A–O

Trạng thái trong bảng chỉ dùng `PASS`, `FAIL`, `BLOCKED`, `NOT_APPLICABLE`. Mỗi hàng liên kết yêu cầu, implementation/test, môi trường bằng chứng, kết quả, mức độ và chủ thể xử lý tiếp. `UNIT_FAKE`, `INTEGRATION_FAKE` và `BROWSER_OFFLINE` chỉ chứng minh fixture/fake tương ứng.

| ID / nhóm | Requirement và nguồn | Component / evidence; environment; expected → actual | Status / severity | Owner và follow-up |
|---|---|---|---|---|
| A — Core Business & Golden | Giữ identity, provenance, metric, date window, nullable score và golden output. Bước 1–10, yêu cầu Bước 21 mục 3. | `test_e2e.py`, `test_golden_contract.py`, `test_refresh_integration.py`; `INTEGRATION_FAKE`. Expected pipeline output ổn định → API/refresh fake và Golden giữ expected, 357 tests toàn bộ pass. | PASS / P1 | Engineering duy trì golden contract; không đổi expected để làm pass. |
| B — Source/Cleaning/Taxonomy | Validate source, normalize, deduplicate đúng scope, phân loại v0.1; giữ zero, missing, censored, partial và 404/429 riêng. | `test_wikimedia.py`, `test_cleaning_pipeline.py`, `test_taxonomy.py`, `test_advanced_quality.py`, `test_e2e.py`; `UNIT_FAKE`, `INTEGRATION_FAKE`. Expected không biến missing thành zero → fixtures kiểm tra status/count/identity theo source/project/ngày. | PASS / P1 | Engineering; dữ liệu Wikimedia vẫn là top-censored `views_ceil`, không phải search volume. |
| C — Algorithm Engine & Evaluation | Registry/version, scoring v0.1, statistical strategies, nullable scores, applicability và không tuyên bố accuracy khi thiếu labels. | `test_algorithm_engine.py`, `test_statistical.py`, `test_algorithm_evaluation.py`, `test_trend.py`, Golden; `UNIT_FAKE`. Expected đúng phép tính và `NOT_COMPARABLE`/`NOT_EVALUATED` → tests pass, không sửa formula hoặc golden. | PASS / P1; accuracy thương mại NOT_APPLICABLE | Product owner + Data/BA cần nhãn độc lập nếu muốn tuyên bố precision/market fit. |
| D — API/Auth/Backward Compatibility | Health, manual run, run/list/detail, auth, paging/search/category, lỗi và Vercel read-only 405. | `test_runs.py`, `test_e2e.py`, `test_frontend.py`, `test_vercel_readonly.py`, `test_composition.py`, Golden; `INTEGRATION_FAKE`. Expected contract hiện hữu → 401 thiếu token, POST thành công chỉ với token giả được cấp trong test, Vercel POST 405, read-only GET không recovery/write. | PASS / P1 | Engineering giữ API contract; auth local/Vercel chưa phải production tenant auth. |
| E — Storage/Firestore/Quota | Tương thích port/schema demo, partial-write handling, idempotency và operation estimates. | `test_repository_contract.py`, `test_storage.py`, `test_storage_analysis.py`, `test_golden_contract.py`; `INTEGRATION_FAKE`. Expected offline semantics → pass, gồm read-after-new-app fake và parent/subcollection semantics. Không có emulator/cloud evidence. | PASS offline; BLOCKED cloud / P1 | Engineering/Cloud owner: Firestore transaction, IAM/quota/storage/egress và emulator cần nghiệm thu riêng. |
| F — Scheduler/Job/Lease/Fencing | Stable schedule occurrence, budget, retry, concurrency, lease takeover, stale owner, cancellation và resource fencing. | `test_job_coordinator.py`, `test_goal_budget.py`, `test_goal_boundaries.py`, `test_goal_concurrency.py`, `test_goal_dispatch.py`, `test_refresh_safety.py`; `INTEGRATION_FAKE`. Expected fake interleavings → pass, worker/scheduler production vẫn không wire. | PASS fake; BLOCKED multi-host / P1 | Engineering/Operations: transaction coordinator, trusted clock, emulator concurrency và host được duyệt. |
| G — Dataset Manifest/Atomic Publish | Persisted shard/digest/provenance verification, strict manifest, READY gate, atomic fake pointer, idempotent lost-ack replay và active fallback. | `test_goal_identity.py`, `test_goal_identity_integration.py`, `test_publish_boundary.py`, `test_goal_replay.py`, `test_refresh_integration.py`; `INTEGRATION_FAKE`. Expected tamper/stale/failure không publish → tests pass; live atomicity chưa có. | PASS fake; BLOCKED cloud / P1 | Engineering/Cloud owner: không thay preflight bằng transaction thật; cần emulator/commit ambiguity. |
| H — Retention/Cleanup | Dry-run mặc định, namespace allowlist, exact paths, active/shared/baseline protection, batches/checkpoint, crash/reload và race với publisher. | `test_retention_offline.py`, `test_research_offline.py`; `INTEGRATION_FAKE`. Expected không xóa active/research/unknown paths → pass; cleanup chưa được wire hoặc gọi cloud. | PASS fake; BLOCKED policy/live / P1 | Chủ sản phẩm + Legal/Engineering duyệt retention/license/grace; Operations xác minh quota trước bất kỳ live delete nào. |
| I — AI/Translation/Enrichment | Provider thật disabled; fake schema/policy/budget/retry/tenant boundaries; sidecar không override rule/entity/score. | `test_ai_offline.py`, `test_translation_offline.py`, `test_enrichment_offline.py`; `UNIT_FAKE`, `INTEGRATION_FAKE`. Expected disabled/no provider call → tests pass; `AI_ENABLED=False`. | PASS offline; BLOCKED provider / P1 | Chủ sản phẩm + Privacy/Legal/Engineering chọn provider, quyền dữ liệu, secret, model, budget và human review. |
| J — Product Research | Attention khác commercial demand; opportunity nullable; evidence/claim-bound; research riêng tenant và tồn tại qua cleanup fake. | `test_research_offline.py`; `INTEGRATION_FAKE`. Expected không bịa sale/price/supplier và demand unknown nếu không evidence → pass; dữ liệu synthetic, không có market validation. | PASS fake; độ chính xác thực tế NOT_APPLICABLE | Product/Research + Legal duyệt source rights, claim và evidence policy; cần nguồn và nhãn thực. |
| K — Dashboard/UX/Accessibility | 10 màn hình, data/error/loading/null/read-only, search/filter/page/detail, XSS/URL, race, keyboard và responsive. | `browser_workspace_offline.py`, `browser_keyboard_offline.py`, `browser_pipeline_e2e_offline.py`, `test_workspace_ui.py`; `BROWSER_OFFLINE`. Expected dashboard hiển thị API data an toàn → pass ở viewport/luồng đã liệt kê. | PASS automated; kiểm tra screen reader BLOCKED / P2 | Frontend owner: manual screen-reader/contrast review còn cần; không tuyên bố WCAG-certified. |
| L — Tenant/RBAC/Secrets/Privacy | Fake server boundary không nhận role/tenant từ caller; secret metadata không nhận raw secret; demo auth tách khỏi production tenant identity. | `test_workspace_security.py`, `test_ai_offline.py`, `test_research_offline.py`, `test_vercel_readonly.py`; `UNIT_FAKE`, `INTEGRATION_FAKE`. Expected cross-tenant deny/fail-closed → pass trong fake; không có trusted production identity/RBAC. | PASS fake; BLOCKED live/commercial / P1 | Security + Product owner: chọn identity provider, server RBAC, sessions, reviewer authority, tenant storage và threat review. |
| M — Reliability/Recovery/Backup | Crash/retry/reload fake, strict manifest/digest sau restore, pointer consistency, idempotent replay và runbook rollback/incident. | `test_goal_replay.py::test_reload_receipt_budget_idempotency_and_fence` (bổ sung đối chiếu manifest/shard digests sau restore), `test_goal_replay.py`, `test_retention_offline.py`; `INTEGRATION_FAKE`. Expected trusted temporary fake backup restore giữ dữ liệu/pointer → pass. | PASS fake; BLOCKED production DR / P1 | Operations/Cloud owner: thiết kế và diễn tập backup/restore cloud; chủ dự án duyệt RPO/RTO. |
| N — Observability/CI/Dependencies | Lệnh kiểm định local fail-nonzero, pinned dependencies, syntax/bundle/browser/network proof và operational signals. | `Backend/scripts/verify_offline_readiness.sh`, `requirements.txt`, `pip check`, tests; local offline. Expected tái chạy → `OFFLINE_READINESS_VERIFICATION=PASS`; không thấy `.github/workflows` được track. 44 requirements exact-version, đều cài và có license metadata; không có advisory DB. | PASS local checks; BLOCKED live monitoring/CVE / P2 | Engineering/Operations: CI proposal cần duyệt riêng; thêm monitoring, alerting và chạy advisory scan khi có DB được phép. |
| O — Production/Commercial Readiness | Hosting, auth/IAM, license/privacy, quota/cost, scheduler, secret, backup, monitoring, SLO/on-call và commercial policy. | Config/code/reports 1–20 cùng test evidence ở trên; không thực hiện cloud/live theo yêu cầu. Expected production evidence → các mục cần quyền/phê duyệt chưa được kiểm chứng. | BLOCKED / P0 trước live/commercial | Product owner + Security/Legal/Cloud/Operations cùng đóng các gate ở mục “Production gates” bên dưới. |

## Core E2E và hợp đồng dữ liệu

Hai tầng E2E hiện có được chạy lại: `Backend/tests/test_e2e.py` đi từ adapter HTTP MockTransport → validation/clean/keyword/score → FakeStorage → run/list/detail API; `test_refresh_integration.py::test_scheduled_real_pipeline_golden_results` đi từ adapter/cleaning/classification/scoring → strict manifest/persisted fake shards → verification/publish. Bổ sung browser integration `Backend/tests/browser_pipeline_e2e_offline.py`: dữ liệu trả từ adapter và RunService được đọc lại qua ASGI API, sau đó cấp chính payload run/list/detail đó cho Chrome bằng bridge fixture; Chrome xác nhận cùng run ID, keyword ID, display title, project và status hiển thị.

Đã kiểm tra semantics: explicit `views_ceil=0` vẫn là 0; null/missing, trang ngoài top, ngày 404, lỗi 429, record bị reject và partial snapshot không bị đổi thành zero/complete; `score=None` giữ null. Cùng article title ở project khác không bị gộp. `views_ceil` chỉ là metric xấp xỉ Pageviews; không được trình bày như Google search volume hoặc purchase demand. Golden expected không đổi.

Browser integration mới sử dụng fixture `golden_v1/source.json` tổng hợp và ASGI/FakeStorage. Nó chứng minh nối hợp đồng ứng dụng trong môi trường offline, không chứng minh live source, Firestore, tính đúng của dữ liệu ngoài hay độ chính xác thương mại.

## API/auth regression

| Surface | Kết quả kiểm tra |
|---|---|
| `GET /api/health` và `/dashboard/` assets | 200/health shape đúng qua ASGI fake. |
| `POST /api/runs` local demo | Không token trả 401; operator test token trả 202; chống run đồng thời/cooldown và lỗi fetch/persist được test. |
| `GET /api/runs/{run_id}` | Trạng thái, counts, coverage, errors; unknown run và storage errors giữ contract. |
| `GET .../keywords` | `all/fashion/home_goods/unclassified`, `q`, sort, limit/offset, empty reason, pagination. Search không fetch source. |
| `GET .../keywords/{keyword_id}` | Baseline, score components, evidence/provenance; null/insufficient/unknown được giữ rõ. |
| Vercel read-only | Middleware ép chỉ đọc và POST trả 405; OIDC identity contract test không fallback ADC; không startup recovery/write worker. |

Không có GET gọi Firestore write hoặc tự Fetch. Basic read auth/operator token là demo auth; không có trusted tenant identity, login rate-limit/session lifecycle đầy đủ hoặc production RBAC.

## Algorithm/evaluation

Scoring demo tiếp tục `wikimedia-trend-v0.1`: current `views_ceil`, median từ các ngày quan sát hợp lệ, tối thiểu baseline theo contract, missing/censored không thay bằng 0; formula và score null được bảo vệ bởi Golden Regression. Engine registry/statistical tests xác nhận strategy IDs/version, applicability, nonfinite/missing, SKIPPED/FAILED và tính tái lập. Evaluation test xác nhận `NOT_COMPARABLE` khi metric/context không tương thích và `NOT_EVALUATED` khi thiếu nhãn.

Volume-only comparison là xếp hạng Pageviews cùng scope; benchmark/evaluation tổng hợp không chứng minh strategy nào tốt hơn và không có nhãn ground truth thực. Không sửa công thức, không scale mọi strategy về một đơn vị chung, không tuyên bố accuracy hoặc product demand.

## Storage, quota và chi phí

Không gọi Firestore, emulator, `gcloud`, Firebase Console hoặc đọc credentials. Các số dưới đây là model offline của Bước 12, không phải billing, usage hay quota hiện tại:

| Model scenario | Estimated operations/payload | Ghi chú |
|---|---:|---|
| Một run | 232 writes; 226 content docs; 9,412,755 JSON bytes | Ước lượng lịch sử từ layout demo; không gồm index encoding/retry overhead. |
| Một run/ngày | 232 writes/ngày; khoảng 9.41 MB JSON/ngày | Storage cloud sẽ khác do document/index metadata. |
| 10 runs/ngày | 2,320 writes/ngày; khoảng 94.1 MB JSON/ngày | Chỉ mô hình. |
| 100 runs/ngày | 23,200 writes/ngày; khoảng 941 MB JSON/ngày | Vượt mốc free writes được ghi trong báo cáo cũ, trước retries; không phải quota check hiện tại. |
| Một keyword-list request | Khoảng 17 document reads (metadata + 16 shards) | Implementation đọc mọi shard rồi filter/page ở Python; `limit=20` không giảm shard reads. |
| Một detail/metadata request | Khoảng 2 / 1 reads | Theo operation model, không tính retry/index reads. |
| 10 viewers × 10 list requests/ngày | Khoảng 1,700 list reads/ngày | Chưa cộng detail, retry, app traffic khác, index reads. |
| Retention deletes / scheduled retries / AI requests | Chưa ước tính đáng tin | Các capability chưa được nối production; không tự giả định sẽ hoạt động. |

Reported free-tier quota trong tài liệu Bước 12 là historical documentation snapshot, không được refresh ở lượt offline này và không chứng minh quota/usage thực tế. Storage 1 GiB, outbound egress, index bytes, hosting terms, provider limits, cold starts và chi phí cần xác minh chính thức trước live. Không bật billing hoặc nâng cấp plan.

### Synthetic API/storage benchmark

`benchmark_storage_offline.py` chạy 10 list requests, 16 shard, limit 50, in-process ASGI + FakeStorage; cache disabled. Số đo thực tế của máy kiểm thử:

| Entities | Estimated fake document reads | HTTP response bytes (10 requests) | Storage JSON returned | elapsed | peak traced memory |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 170 | 251,259 B | 5,022,800 B | 0.2147 s | 1,620,884 B |
| 5,000 | 170 | 252,320 B | 25,192,550 B | 0.8964 s | 2,746,680 B |
| 10,000 | 170 | 252,605 B | 50,404,800 B | 1.7987 s | 5,089,372 B |

Đây là fake/local timing và JSON-size estimates, không phải Firestore latency, wire bytes, egress, SLO hay performance production. Kết quả xác nhận rủi ro đọc hết shard khi phân trang/search.

## Job coordination, publish và recovery

Tests kiểm tra schedule UTC/7 ngày, stable occurrence/dedup dispatch, budget không cấp lại, 2 threads tranh lease, takeover/stale owner, fencing/resource token, revision CAS, cancellation, Retry-After/deadline, retry budget exhausted, crash checkpoint, strict manifest và tamper, shard digest/count/provenance, lost publish acknowledgement, replay cũ không rewind active mới. Fake concurrency dùng thread/barrier và lock thật trong cùng process; tests không dùng sleep làm bằng chứng race duy nhất.

Các trạng thái runtime production của scheduler/worker không bật. `FakeScheduler`, `FakeCoordinator` và `FakeDatasetStore` sử dụng in-process state/RLock và kiểm tra logic fake; không có Firestore transactional coordinator, emulator two-client contention, trusted time/skew handling hoặc multi-host failure test. COMPLETED fake publication không được hiểu là cloud atomic.

## Retention và cleanup

Retention tests kiểm dry-run mặc định, allowlist namespace, ID/path validation, parent xóa cuối, active pointer, shared/baseline reference, unknown subcollection, license/legal conflict, batch budget, cursor/reload/replay, cancellation và publish-cleanup race. User research nằm namespace fake riêng và test chứng minh cleanup dataset không xóa saved research fake.

Không có cleanup Firestore adapter, endpoint, TTL hoặc production scheduler. Ngưỡng 7 ngày + grace 7 ngày chỉ là synthetic policy; retention/legal/license và thời gian giữ audit chưa được chủ dự án phê duyệt. Không có cloud delete trong Bước 21.

## AI, translation, enrichment và Product Research

AI production `DISABLED`; test dùng FakeProvider và kiểm strict schema, unknown provider/price, permission, data fingerprint, budget/retry, malformed JSON, timeout, prompt injection boundary, tenant, redacted audit, translation review-required, NER/semantic candidate review và không override rule/entity/score. Không có provider API key được đọc hoặc provider thật được gọi. Fixture output không chứng minh model thật chính xác.

Research test chỉ dùng synthetic evidence/fake repository; Wikimedia attention không tạo demand, sale, price, supplier hoặc opportunity score nếu thiếu commercial evidence. Review là fake, không phải reviewer/RBAC production. Không marketplace live và không có market-quality evaluation.

## Dashboard/browser và accessibility

Chrome suites chạy file fixture và API fake dưới launcher seccomp Unix-only, bỏ biến proxy, Chrome tắt background networking/component update/sync, không mở server hoặc internet URL. Kiểm tra:

- 10 navigation views, DOM/accessibility labels, run/list/detail, filter, search, pagination và request race.
- Loading/empty/error/partial/stale/null score, source/refresh disabled, synthetic preview labels, read-only/config error và không POST.
- XSS/untrusted text, malicious URL, không dùng local/session storage, chi tiết dialog.
- Viewport 320/400 CSS px thực với DevTools, 500/768/1280 browser widths; Chrome native window-size yêu cầu 400 px được đo thành 500 px ở DOM suite.
- Tab/Enter/Escape, focus, labels, table header, overflow và live status.
- Integration Chrome đọc đúng row từ adapter/API fake nói trên.

Không manual test screen reader, chưa rà contrast toàn bộ và chưa có chứng nhận WCAG 2.2 AA. Các capability chưa có backend production tiếp tục disabled hoặc gắn `OFFLINE DEMO / SYNTHETIC DATA`.

## Tenant/auth/security review

Fake security suite chứng minh identity/membership do test harness đăng ký, role/action/resource checks, denial cross-tenant, expired fake identity, review authority và secret metadata không chứa secret value. Research fake không lẫn shared dataset; query/header không thể tự nâng role trong test. Các role tên `platform_admin`, `reviewer`, v.v. là demo policy, không được nối runtime.

Audit tĩnh không tìm thấy `innerHTML`, `localStorage`, `sessionStorage` hoặc periodic `setInterval` trong Dashboard/runtime paths đã quét; URL evidence allowlist HTTPS Wikimedia family. Run ID/keyword IDs được validate, data hiển thị bằng `textContent`/DOM API. Không in hoặc mở file `.env`, ADC hay service account. Quét high-confidence secret patterns chỉ báo path nếu match; không đưa giá trị vào report. Đây không thay thế secret scanner đầy đủ hoặc security review độc lập.

Demo Basic auth và operator token có giới hạn; `/api/health` công khai theo thiết kế. Tenant auth, session expiry/revocation production, CSRF/rate-limits, identity provider, server RBAC, tenant-aware storage/index, secret manager/rotation, IAM, reviewer separation và threat model đều là gate trước live/commercial.

## Dependencies, CI và observability

`Backend/requirements.txt` pin chính xác 44 packages; toàn bộ 44 installed và metadata có khai báo license; `pip check` không có dependency conflict. Không có lockfile/hash lock; pyproject là cấu hình Vercel, không thay requirements pin. License metadata được tổng hợp tại môi trường local, chưa có legal compatibility/NOTICE review.

`pip-audit`, `shellcheck`, `trivy` không có trong PATH. Không tải package hoặc advisory database. Kết quả: **`VULNERABILITY_SCAN_NOT_VERIFIED`**; `pip check` chỉ là consistency check, không chứng minh absence of CVEs. Không tìm thấy tracked `.github/workflows`; không tự tạo workflow sẽ tự chạy khi push.

Thêm lệnh local `Backend/scripts/verify_offline_readiness.sh`: strict seccomp probe, full unittest, Golden hai seed, pip check, shell/JS syntax, bundle byte comparison và cleanup, ba Chrome suites, benchmark fake. Script dùng `set -euo pipefail`, dừng với nonzero khi lỗi, không cài dependency hoặc gọi service. Monitoring backend/live alerts/on-call chưa có; `MONITORING_LIVE_NOT_CONFIGURED`. Run IDs/status/coverage/errors có trong API; metrics storage counters chỉ là decorator/offline estimate, không phải production telemetry.

## Network isolation và no-live evidence

Strict launcher `Backend/scripts/seccomp_exec.py` đặt `PR_SET_NO_NEW_PRIVS`, seccomp từ chối socket AF_INET/AF_INET6/non-Unix, mọi `connect` và `io_uring_setup`, đóng inherited FDs 3 trở lên, rồi `exec` command; filter được tiến trình con kế thừa. `probe_network_isolation.py` kiểm Python socket, libc socket, libc connect và child Python đều nhận EPERM cho internet; AF_UNIX tạo socketpair được phép để asyncio chạy, nhưng strict mode từ chối `connect`.

Chrome cần AF_UNIX cho IPC; browser mode cho AF_UNIX socket/connect, vẫn chặn AF_INET/AF_INET6/những socket family khác và `io_uring_setup`. Browser tests tự xác nhận AF_INET/AF_INET6 bị chặn, dùng fixture `file://`, proxy variables bị loại và Chrome tắt background networking. Vì vậy browser sandbox cho phép local Unix IPC; nó không phải sandbox production. Không dùng network namespace (`unshare -n` không được kiểm tra trong Bước 21), không tuyên bố isolation từ log vắng request.

Trong lượt Goal Bước 21: không chạy Firestore client, cloud CLI, Wikimedia/AI/marketplace client live, Firebase Console, deployment hay billing. Source tests dùng `httpx.MockTransport`, ASGI dùng `httpx.ASGITransport`; no-network probe và browser tests là bằng chứng chặn kết nối ở syscall layer. Không có live request hoặc cloud mutation được thực hiện.

## Backup/restore, release, incident và rollback runbook

Offline test `test_goal_replay.py::test_reload_receipt_budget_idempotency_and_fence` ghi trusted fake state vào tempfile, tạo fake coordinator/repository mới, kiểm manifest/shard digests sau restore, xác nhận dataset ACTIVE và active pointer revision giữ nguyên, replay publication cũ idempotent, rồi publish dataset mới; replay cũ không ghi lùi pointer. Đây là pickle của state tự tạo/đáng tin cậy trong test. Không dùng pickle cho user input. Test chứng minh fake state replay, không phải cloud backup/restore.

Runbook trước production:

| Incident | Hành động an toàn trước mắt | Điều kiện khôi phục |
|---|---|---|
| Bad deployment/config | Tắt traffic/fetch theo change control, giữ nguyên dữ liệu và lưu run/deploy IDs, config version, errors. | Rollback code/config đã biết tốt; không rollback pointer/dataset bằng cách xóa hoặc replay tùy tiện. |
| Partial publish/storage corruption | Dừng worker/write path; giữ active dataset cũ; thu thập manifest, shard digest và job/occurrence evidence. | Restore vào namespace mới, verify digest/manifest/pointer trước cutover; live procedure chưa được nghiệm thu. |
| Worker crash/lease loss | Không cho stale worker publish; giữ draft/audit để đối chiếu. | Retry idempotent cùng occurrence sau khi lease/fence thật được kiểm chứng; hiện chỉ fake. |
| Source outage/429/invalid | Không phát hành snapshot mới như hoàn chỉnh; giữ coverage/error, tôn trọng bounded retry/Retry-After. | Chạy lại bằng operator approval sau khi source hồi phục; không thay missing bằng zero. |
| Quota exhausted/cleanup request | Dừng write/delete, giữ checkpoint và active pointer; không cấp budget mới tự động. | Chủ vận hành xác minh usage, namespace, backup, retention/legal policy và approval. |
| AI outage/credential exposure | AI đang disabled; không log secret. Nếu sau này lộ secret, revoke/rotate qua secret manager và điều tra audit. | Production provider chưa có, secret rotation procedure phải được thiết kế/diễn tập. |
| Tenant data leakage | Khoanh vùng session/resource, tạm khóa access và preserve audit. | Trusted identity/RBAC review, containment và privacy/legal response trước khi mở lại. |
| Stale data/monitoring alert | Ghi rõ observed date/coverage, không gọi dữ liệu cũ là mới. | Owner duyệt refresh run; freshness policy và alert thresholds cần định nghĩa. |

RPO/RTO chưa được chủ sản phẩm duyệt. Backup, restore, rollback, incident response và cross-tenant recovery production chưa được diễn tập. No cloud export/restore/mutation.

## Production readiness gates

| Gate | Trạng thái | Bằng chứng còn thiếu / owner |
|---|---|---|
| Hosting plan/terms, runtime, network/firewall | BLOCKED | Product/Operations duyệt host thương mại và xác minh giới hạn/version hiện hành; không coi Vercel Hobby đã được chấp thuận. |
| Authentication, tenant authorization, reviewer authority | BLOCKED | Security/Product thiết kế trusted identity, RBAC, session/revocation và cross-tenant security test. |
| Firestore IAM, ADC/WIF, read/write separation | BLOCKED | Cloud owner xác minh principal/least privilege thực tế; không suy ra từ code hoặc ADC máy developer. |
| Firestore transactions, lease, fencing, atomic publish | BLOCKED | Engineering xây adapter được duyệt, emulator cạnh tranh và unknown-commit tests; fake RLock không đủ. |
| Quota, storage bytes/indexes, egress, cost | BLOCKED | Operations đo Usage/storage hợp lệ và duyệt budget trước workload; model Bước 12 không phải usage. |
| Scheduler host, worker, retry, cancellation | BLOCKED | Chọn runtime/operator owner, idempotency durable và recovery; hiện production disabled. |
| Retention/delete permissions và policy | BLOCKED | Chủ dự án/Legal duyệt license, retention, grace, legal hold, audit và user research; không bật TTL. |
| Source terms/license and market claims | BLOCKED | Legal/Product xác nhận quyền lưu/hiển thị/redistribute và phạm vi tuyên bố; Pageviews không phải demand. |
| Privacy, secret manager, provider/model, AI budget | BLOCKED | Security/Privacy/Product duyệt dữ liệu, PII, provider, retention, cost cap, rotation và review. |
| Backup/restore, rollback, incident response | BLOCKED | Cloud/Operations diễn tập restore, pointer/digest integrity, RPO/RTO và no-cross-tenant recovery. |
| Monitoring/on-call, alerts, SLO/capacity | BLOCKED | Operations cấu hình metrics/alerts/ownership và live load baselines; hiện `MONITORING_LIVE_NOT_CONFIGURED`. |
| Commercial readiness, policies, legal terms | BLOCKED | Chủ sản phẩm/Legal/Security duyệt terms, payments, support, privacy, licensing và product claims. |

Không có placeholder nào được tính là PASS; không bật bất kỳ gate nào để lấy PASS.

## Failure Matrix (50 invariants)

`PASS` ở đây có nghĩa test/evidence offline tương ứng chạy thành công; không nâng cấp thành cloud/live proof. Mục nào chỉ có fake thì ghi rõ giới hạn trong bằng chứng và production gates.

| # | Expected invariant | Evidence đã chạy / kết quả | Status |
|---:|---|---|---|
| 1 | Golden scoring giữ nguyên | `test_golden_contract.py`, 7/7 với seed 1 và 983; expected không sửa. | PASS |
| 2 | API backward compatibility | `test_runs.py`, `test_e2e.py`, `test_frontend.py`, Golden. | PASS |
| 3 | Vercel POST bị từ chối | `test_vercel_readonly.py`; 405 trong read-only. | PASS |
| 4 | Unauthorized manual run | `test_runs.py`, `test_e2e.py`; thiếu token 401/fail-closed. | PASS |
| 5 | Missing date không thành zero | `test_wikimedia.py`, `test_trend.py`, Golden. | PASS |
| 6 | Explicit views=0 được giữ | `test_wikimedia_cleaning.py`, `test_trend.py`, `test_refresh_integration.py`. | PASS |
| 7 | Not-in-top/censored không thành zero | `test_trend.py`, `test_advanced_quality.py`. | PASS |
| 8 | Partial snapshot không là complete | `test_trend.py`, `test_e2e.py`, `test_refresh_integration.py`. | PASS |
| 9 | Source 404 giữ unavailable | `test_wikimedia.py`, `test_e2e.py`, `test_refresh_integration.py`. | PASS |
| 10 | Source 429/Retry-After bounded | `test_wikimedia.py`, `test_refresh_integration.py`. | PASS |
| 11 | Normalize/dedup identity đúng project/ngày | `test_wikimedia_cleaning.py`, `test_keywords.py`, `test_trend.py`. | PASS |
| 12 | Algorithm SKIPPED không giả score | `test_algorithm_engine.py`, `test_algorithm_evaluation.py`. | PASS |
| 13 | Algorithm FAILED được phân biệt | `test_algorithm_engine.py`, `test_algorithm_evaluation.py`. | PASS |
| 14 | Metric không tương thích trả NOT_COMPARABLE | `test_algorithm_evaluation.py`, enrichment evaluation regression. | PASS |
| 15 | Storage read/write error không báo success | `test_storage.py`, `test_runs.py`, `test_e2e.py`. | PASS |
| 16 | Quota budget hết fail-closed fake | `test_goal_budget.py`, `test_goal_boundaries.py`, `test_storage_analysis.py`. | PASS |
| 17 | Hai worker tranh lease | `test_goal_concurrency.py`, `test_job_coordinator.py`; thread/barrier fake. | PASS |
| 18 | Stale owner không ghi tiếp | `test_goal_concurrency.py`, `test_refresh_safety.py`. | PASS |
| 19 | Resource fencing ngăn worker cũ | `test_goal_identity_integration.py`, `test_goal_concurrency.py`. | PASS |
| 20 | Manifest tampering bị từ chối | `test_goal_identity.py`, `test_publish_boundary.py`. | PASS |
| 21 | Partial writes không COMPLETED | `test_runs.py`, `test_golden_contract.py`, refresh safety. | PASS |
| 22 | Crash trước publish không đổi active | `test_goal_replay.py`, `test_refresh_integration.py`. | PASS fake |
| 23 | Lost publish acknowledgement replay an toàn | `test_goal_replay.py::test_lost_ack_replay_after_newer_publication`. | PASS fake |
| 24 | Retry publication cũ không rollback pointer | `test_goal_replay.py`, active dataset mới vẫn giữ nguyên. | PASS fake |
| 25 | Refresh fail giữ active cũ | `test_refresh_integration.py`, `test_refresh_safety.py`. | PASS fake |
| 26 | Duplicate schedule/occurrence không tạo budget mới | `test_goal_dispatch.py`, `test_goal_budget.py`. | PASS fake |
| 27 | Cleanup default dry-run | `test_retention_offline.py`. | PASS fake |
| 28 | Active dataset được bảo vệ | `test_retention_offline.py`. | PASS fake |
| 29 | Shared/baseline reference bảo vệ cleanup | `test_retention_offline.py`. | PASS fake |
| 30 | Cleanup xóa child trước parent, không đụng subcollection lạ | `test_retention_offline.py`; exact namespace. | PASS fake |
| 31 | Cleanup crash/reload/replay idempotent | `test_retention_offline.py`. | PASS fake |
| 32 | Job budget exhausted không publish hoặc reset ngầm | `test_goal_budget.py`, `test_goal_boundaries.py`. | PASS fake |
| 33 | AI disabled không gọi provider | `test_ai_offline.py`, `test_translation_offline.py`, `test_enrichment_offline.py`, `test_research_offline.py`. | PASS fake |
| 34 | AI invalid schema bị reject | AI/translation/enrichment schema tests. | PASS fake |
| 35 | Translation unavailable/review-required không thành verified | `test_translation_offline.py`, `test_enrichment_offline.py`. | PASS fake |
| 36 | Unsupported enrichment evidence không thành approved fact | `test_enrichment_offline.py`. | PASS fake |
| 37 | Research demand unknown khi thiếu evidence | `test_research_offline.py`. | PASS fake |
| 38 | Cross-tenant fake access bị từ chối | `test_workspace_security.py`, `test_research_offline.py`, AI tests. | PASS fake |
| 39 | Secret metadata/audit không lộ raw secret | `test_workspace_security.py`, `test_ai_offline.py`, translation tests. | PASS fake |
| 40 | Dashboard preview gắn nhãn synthetic | `browser_workspace_offline.py`, `test_workspace_ui.py`. | PASS browser |
| 41 | Dashboard pagination đúng | `browser_workspace_offline.py`, `test_runs.py`, `test_e2e.py`. | PASS browser/API |
| 42 | Dashboard request race không ghi đè response mới | `browser_workspace_offline.py` và API sequence behavior. | PASS browser |
| 43 | Dashboard XSS không tạo DOM node | `browser_workspace_offline.py`; text-only rendering. | PASS browser |
| 44 | URL nguy hiểm/host giả không thành evidence link | `browser_workspace_offline.py`; scheme/host matrix. | PASS browser |
| 45 | Keyboard/responsive checks | Chrome DevTools 320/400 CSS px, Tab/Enter/Escape; 500/768/1280. | PASS browser |
| 46 | Read-only browser không submit POST | `browser_workspace_offline.py` read-only/config-error modes. | PASS browser |
| 47 | Trusted fake backup restore giữ manifest/digest/pointer | `test_goal_replay.py::test_reload_receipt_budget_idempotency_and_fence`; restore/replay và active mới. | PASS fake |
| 48 | Không có cloud/live operation trong lượt này | Strict seccomp probe, child, ASGI/MockTransport, browser sandbox; không gọi provider/cloud. | PASS offline |
| 49 | Offline verification runner trả exit code đúng | `verify_offline_readiness.sh` hoàn tất cuối cùng và in `OFFLINE_READINESS_VERIFICATION=PASS`. | PASS local |
| 50 | Configuration fail-closed cho capability chưa có | `test_vercel_readonly.py`, `test_composition.py`, AI disabled/default and workspace tests. | PASS offline |

Giới hạn shared: concurrency/publish/cleanup/backup/tenant/AI tests là fake; cloud equivalents chưa nghiệm thu.

## Test và validation cuối

Lệnh kiểm định thống nhất:

```bash
cd /mnt/Data/Mine/TrendScout
./Backend/scripts/verify_offline_readiness.sh
```

Kết quả lượt cuối trước khi lập báo cáo:

- **357/357 unittest pass**, không fail/skip.
- Golden `test_golden_contract.py`: **7/7** với `PYTHONHASHSEED=1`, **7/7** với `983`.
- `pip check`: `No broken requirements found.`
- `bash -n setup.sh start.sh Backend/scripts/verify_offline_readiness.sh`: pass.
- `node --check Frontend/public/app.js`: pass.
- Vercel bundle `index.html`, `app.js`, `styles.css`: byte-for-byte khớp source; artifact tạm được xóa.
- Chrome DOM suite: 400-requested/500 measured, 768, 1280; read-only true/config-error; pass.
- Chrome keyboard suite: 320/400 CSS px, Tab/Enter/Escape; pass.
- Chrome pipeline/API/dashboard integration: cùng keyword ID/title/project/status; pass.
- Synthetic benchmark: 1k/5k/10k; pass.
- Strict no-network probe: Python/libc internet sockets, native connect và child bị EPERM; browser mode chỉ cho Unix IPC và block internet families.
- Golden Expected không thay đổi; không có API/Firestore/scoring/classifier migration.

`unshare -n` không được dùng. ShellCheck và vulnerability audit tool/advisory database không có sẵn; không tải/cài từ Internet.

## Files thay đổi

- `Backend/scripts/seccomp_exec.py` — launcher seccomp strict và Unix-only cho Chrome IPC.
- `Backend/scripts/probe_network_isolation.py` — kiểm syscall Python/libc/child.
- `Backend/scripts/verify_offline_readiness.sh` — tái chạy suite offline, checks, browser tests, bundle và benchmark.
- `Backend/scripts/benchmark_storage_offline.py` — thêm workloads 10,000 entities.
- `Backend/tests/browser_pipeline_e2e_offline.py` — adapter→ASGI API→Chrome integration với payload thật từ fixture pipeline.
- `Backend/tests/test_goal_replay.py` — xác minh manifest/shard digest và pointer consistency sau restore fake.
- Báo cáo này.

Không có source production API, Firestore schema, scoring/classification, scheduler wiring hoặc Dashboard source code được sửa. Không đưa credential, `.env`, generated bundle, profile hoặc benchmark output vào repo.

## Risks và hành động tiếp theo

| Priority | Risk/gate | Owner | Điều kiện đóng |
|---|---|---|---|
| P0 trước live | Firestore transaction, atomic pointer, lease/fencing, quota/IAM | Cloud + Backend | Adapter/emulator multi-client tests, IAM evidence, usage/cost approval và rollback test. |
| P0 trước thương mại | Trusted tenant identity/RBAC, privacy, source/data rights | Security + Product + Legal | Threat review, policy/terms approvals và adversarial tenant tests trên production boundary. |
| P1 | Backup/restore/monitoring/on-call chưa có bằng chứng live | Operations | Diễn tập restore/incident, RPO/RTO, alerts, ownership và SLO được duyệt. |
| P1 | Keyword list đọc toàn bộ 16 shards mỗi request | Backend + Product | Query/cursor/cache design được duyệt, benchmark với Firestore thật sau quota approval. |
| P1 | Dashboard manual screen reader/contrast review còn thiếu | Frontend | Manual assistive-technology review; không tuyên bố WCAG certification trước đó. |
| P2 | Không có advisory scan offline; requirement versions pin nhưng chưa hash-lock | Backend/DevOps | Chọn lock/hash process và chạy vulnerability scan từ advisory DB được phê duyệt. |
| P2 | Hai roadmap guide, D1–D12 registry không có trong repo | Chủ dự án/BA | Khôi phục tài liệu nguồn hoặc xác nhận vị trí chính thức, ghi quyết định thật sau approval. |

**Go/no-go:** GO cho MVP offline demo nội bộ theo giới hạn hiện có; NO-GO cho live scheduled ingestion, cloud publish/cleanup, production multi-tenant, AI provider hoặc commercial claims. `BLOCKED_FOR_LIVE` và `COMMERCIAL_NOT_READY` được giữ nguyên.

## Git

Sau kiểm tra diff/secrets/artifacts và `git diff --cached --check`, thay đổi code/tests được commit local trước báo cáo; báo cáo được commit local riêng sau đó theo hai message đã yêu cầu. Không push, force-push hoặc rewrite history.
