# Giai đoạn 2 — Bước 16: AI provider và safety guards

**Kết luận: `OFFLINE_ACCEPTED — PROVIDER_PENDING_APPROVAL`; `BLOCKED_FOR_LIVE`.** Đây là prototype độc lập chỉ được thử offline bằng fixture tổng hợp, không kết nối vào pipeline hoặc runtime hiện tại. Không gọi provider thật, không đọc/tạo API key; chi phí live AI trong bước này là **0**. Không triển khai Translation, Classification, Product Research hoặc workflow AI nghiệp vụ.

## Kiến trúc và contract

`app/ai/contracts.py` định nghĩa provider protocol, metadata, bảy task type, request, response, usage và mã lỗi; `provider.py` chứa registry và fake provider, không có SDK hay client mạng. Metadata fake khai báo phiên bản provider/model, task, strict-json, giới hạn context/output, timeout/cancellation, usage và lớp lỗi. Registry từ chối ID trùng; provider/model hoặc task không biết fail-closed. Module không được composition root import, không tự gọi provider; `AI_ENABLED=False`, provider thật `UNCONFIGURED`. `SecretResolver` chỉ là contract tương lai server-only, chưa resolve, lưu, xoay hay thu hồi credential thực.

OpenAI và Gemini mới là **đặc tả adapter tương lai**: adapter cần cung cấp metadata và ánh xạ lỗi, hỗ trợ timeout/cancellation thực tế, tách source/instruction, trả JSON và usage về engine, nhận secret qua server-only resolver. Không suy diễn API, tính năng, giá, điều khoản, vùng xử lý hoặc khả năng tương thích provider từ fake. Chưa import SDK và chưa tạo adapter thật.

Request dùng version `synthetic-request-v1`, response `synthetic-v1`: JSON object chỉ có `kind=synthetic`, `items`, tối đa 20 item với `label` enum và `value` string; cấm field thừa, field trùng, sai kiểu/enum, Unicode lỗi, truncated/refusal, JSON hỏng và output trên 8192 byte. Đây là schema minh họa contract, không phải schema nghiệp vụ hoặc kết quả dịch/phân loại. Mọi SUCCESS đều qua validate; malformed không được sửa tự động. Output chứa lệnh được coi là chuỗi, không có tool executor, shell, SQL, URL fetch hay quyền Firestore.

## Trust boundary, budget, cache, audit

`Authority` cấp context tenant giả do code test tin cậy inject; không phải authentication hay chứng minh membership. `Policy` và permission/source permission version cũng là input tin cậy từ caller: nguồn/provider không được cấp quyền đặt chúng trong một tích hợp thật. Guard kiểm tra context, task, danh mục dữ liệu, permission, fingerprint, các version, deadline, giới hạn kích thước và token trước call. Instruction cố định tách riêng source data không tin cậy; cách này giới hạn quyền, **không phát hiện được mọi prompt injection**.

Mỗi tenant/job có account trong `State` và RLock; reserve request, input/output token và cost dự kiến trước mỗi invocation. Ước lượng token là số do test cung cấp, không phải tokenizer thật. Chỉ settle về usage khi usage là số nguyên hợp lệ trong reservation. Thiếu usage, timeout, lỗi hay acknowledgement không rõ giữ reservation; retry có giới hạn và reserve mới, chỉ retry lỗi transient/429 khi fake xác nhận, dùng backoff và Retry-After theo fake clock. Unknown price là `COST_UNKNOWN`; nếu có cost cap mà không biết giá thì từ chối. Price `SYNTHETIC` tính bằng đơn vị giả, không phải USD/bảng giá provider. Idempotency ràng buộc context, fingerprint và version; snapshot/restore chỉ dùng đối tượng tin cậy trong bộ nhớ test, không đọc file pickle hay chứng minh durability. Một process mới chỉ giữ budget nếu được cấp lại state đã checkpoint; state mới trống không phải cơ chế quota production. Cancellation được kiểm tra trước và sau call giả, không chứng minh hủy được request provider thật.

Cache tắt mặc định; khi bật trong test, key gồm tenant/job, task, provider/model/version, template/schema/request/policy/permission version, fingerprint, categories, ngôn ngữ và giới hạn token. Có TTL và LRU bound. Chỉ những giá trị thuộc allowlist fixture tổng hợp cố định được cache; invalid output và policy denial không cache. Không có shared scope hoặc cache production. Idempotency giữ kết quả trong fake state để replay, vì vậy snapshot này không được xem là cơ chế lưu trữ an toàn cho dữ liệu thật. Không có bằng chứng cache production an toàn.

Audit chỉ chứa mã outcome, latency fake, cache hit/miss, usage estimated/reported, accounting và các identifier đã hash; không ghi raw prompt, response, Authorization hoặc exception provider. Hash identifier không phải cơ chế ẩn danh tuyệt đối. Các exception validation và provider được chuẩn hóa. `Result.availability` báo `SKIPPED` hoặc `AI_UNAVAILABLE` cho lỗi; caller tương lai phải quyết định fallback core một cách tường minh.

## Ma trận kiểm thử

Các test trong `Backend/tests/test_ai_offline.py` dùng dữ liệu và giá tổng hợp:

| Invariant | Test |
|---|---|
| Đường đi đủ guard → fake → validate → usage → audit | `test_happy_path_accounting_and_default` |
| JSON, enum, field, kích thước, Unicode, refusal/truncation | `test_strict_validation`, `test_refusal_permanent_and_registry` |
| Tenant giả mạo, permission, category, model/task lạ | `test_policy_forgery_and_unknown_provider`, `test_idempotency_and_cross_tenant_budget` |
| Prompt injection / lệnh output không thực thi | `test_prompt_injection_is_data_only`, `test_source_permission_and_untrusted_tool_output` |
| Token, request, cost, retry, unknown ack/usage, reload | `test_budget_boundaries_no_reset`, `test_retry_budget_and_unknown_ack`, `test_retry_exhaustion_is_bounded`, `test_cost_and_missing_usage`, `test_restore_keeps_reservations_and_replay`, `test_invalid_request_schema_and_synthetic_cost_cap` |
| Timeout, cancellation, exception và audit không rò fixture secret | `test_timeout_cancellation_redaction`, `test_provider_exception_is_sanitized_and_no_retry_unknown_ack` |
| Cache tenant/version/TTL/eviction và secret | `test_cache_tenant_isolation_version_ttl_and_invalid_output`, `test_cache_eviction_and_permission_version`, `test_cache_never_stores_unapproved_response_content` |
| Đồng thời/rate trong process | `test_concurrency_and_rate_limit` |

Kết quả cuối: **286/286 tests pass** (bao gồm 19 test AI; baseline 267), Golden Regression **7/7** với mỗi seed 1 và 983, `pip check` không có dependency lỗi, `bash -n setup.sh start.sh` và `node --check Frontend/public/app.js` pass. Không sửa Golden Expected. Test chạy qua launcher seccomp chặn `socket`, `connect`, `io_uring_setup`, đóng file descriptor kế thừa; xác nhận socket Python và native bị EPERM và child process cũng bị chặn. Launcher là công cụ tạm ngoài repo, không phải cơ chế bảo mật production.

Không thay các file API/Firestore demo, manual-run, HostRunLock, Vercel read-only, taxonomy, cleaning/scoring v0.1, Algorithm Engine, scheduler/worker hoặc retention; regression hiện có vẫn pass. Không bật background job, deploy, billing, IAM hay ghi/xóa cloud.

## PENDING_APPROVAL và giới hạn trước live

Cần chủ dự án duyệt provider/model, dữ liệu và license được gửi, policy chia sẻ/retention/training, budget, giá, secret management và quyền tenant. Cần thiết kế authenticating server boundary, credential rotation/revocation, quota và idempotency durable phân tán, observability bảo mật, timeout/cancellation thật, policy chống prompt injection theo task và schema nghiệp vụ. Cần kiểm thử provider live riêng, IAM, hosting, billing/quota và production rollback trước khi sử dụng. RLock, fake clock, fake usage, cache và checkpoint chỉ chứng minh hành vi một tiến trình; không chứng minh race đa host, tính bền vững hay chi phí thực. **`BLOCKED_FOR_LIVE` giữ nguyên.**
