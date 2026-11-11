# Báo cáo D5 — đang thực hiện

**Trạng thái: D5 chưa đạt; `D5_BLOCKED_PRODUCTION_INFRASTRUCTURE` cho phần runtime phân tán, `D6_PROVIDER_ONBOARDING_PENDING`, `LIVE_ACTIVATION_NOT_YET_ACCEPTED`.** Không đánh dấu `D5_LIVE_CAPABLE_OFFLINE_VERIFIED`, `DEMO_READY_FOR_PRESENTATION` hay `FOUR_SOURCE_LIVE_VERIFIED` khi các điều kiện còn mở. Quyết định mới: D5 là foundation live-capable và demo offline; D6 là account/credential của tổ chức, admin riêng và kích hoạt live; D7 nghiệm thu live cuối. Không có D3C.

## Phần đã thực hiện

- `bash demo.sh` chạy local loopback với synthetic fixture và `FakeStorage` tạm; không đọc cloud project, credentials hay operator token từ môi trường. Fetch ẩn danh dùng backend và dữ liệu được gắn synthetic. Hiện chỉ fixture Wikimedia chạy trong pipeline; không quảng cáo thành demo tích hợp bốn nguồn. `start.sh` không còn mặc định một project cloud.
- `CredentialProvider` là port backend dùng chung cho clients YouTube và SerpApi; các adapter vẫn chỉ chấp nhận MockTransport. Đây chưa phải secret manager hoặc triển khai live-capable hoàn chỉnh. Legacy endpoint không được mở anonymous.
- Module discovery offline có extraction, provenance, phân vùng source/scope/mode, baseline, giới hạn chứng cứ, priority và gợi ý nghiên cứu. Origin và cached delivery tách nhau. Module chưa nối vào pipeline hoặc dashboard và chưa được chứng minh bằng dữ liệu live.
- Đã cập nhật [kiến trúc và acceptance matrix](../Design/D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md), [approval register](../Design/D5_FOUR_SOURCE_LIVE_APPROVAL_REGISTER.md), [thiết kế quota/queue](../Design/D5_QUOTA_CACHE_QUEUE_AND_ADMISSION_DESIGN.md), [discovery](../Design/D5_KEYWORD_EXTRACTION_AND_TREND_DETECTION.md) và [kế hoạch D6](../Design/D6_PROVIDER_ACCOUNT_CREDENTIAL_AND_LIVE_ACTIVATION_PLAN.md). Quyền sở hữu Firebase hiện chưa xác minh; nếu thuộc cá nhân cần ghi nhận rủi ro và đề xuất chuyển giao được duyệt riêng.

## Bằng chứng offline

`bash Backend/scripts/verify_offline_readiness.sh` PASS: 476 unittest, Golden với hai hash seed, dependency check, bundle parity, browser UI/keyboard/responsive, synthetic pipeline và one-click synthetic Fetch, benchmark synthetic. Test demo mới kiểm tra API, fixture và cloud config dưới network isolation; các browser harness hiện có chưa chứng minh launcher demo thật trong trình duyệt. Ruff cho các file Python mới/sửa và `git diff --check` PASS. Không gọi live provider, Firestore hay emulator; không phát sinh provider cost.

## Công việc và giới hạn còn lại

1. Làm live-capable transport có kiểm soát cho Google, Bing, YouTube và nối credential backend, nhưng giữ live disabled cho đến D6. Wikimedia live chưa được duyệt hoặc xác minh.
2. Xây dựng và kiểm chứng admission/queue/cache/quota phân tán, crash recovery, fencing, cost accounting và Storage Port cho toàn bộ D5. Firestore coordinator hiện chỉ guarded emulator; fake/in-memory không chứng minh multi-host. Không migration database khi chưa có quyết định riêng.
3. Tích hợp bốn fixture synthetic vào workflow, discovery và dashboard; phân biệt origin `LIVE`/`SYNTHETIC`, delivery `CACHED` và trạng thái `INSUFFICIENT_EVIDENCE` xuyên API và UI. Kiểm thử browser với ứng dụng demo thật và hoàn thành checklist presentation.
4. Chạy lại full regression sau các thay đổi tiếp theo, review bảo mật, hoàn thiện blueprint triển khai và cập nhật ma trận trước khi nghiệm thu D5.

Không deploy, push, tạo account/dịch vụ trả phí, dùng credential cá nhân hoặc bật live. D6 cần chủ sở hữu tổ chức, quyền sử dụng và budget trước khi setup; D7 cần bằng chứng live độc lập.
