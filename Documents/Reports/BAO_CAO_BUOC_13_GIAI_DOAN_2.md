# Giai đoạn 2 — Bước 13: Job coordination

Kết quả: contract và fake coordinator kiểm thử offline; **BLOCKED_FOR_LIVE / multi-host**. Không kết nối coordinator vào app, không worker, scheduler, Firestore adapter hay publish thật. Không thay API, schema, scoring, `HostRunLock`, Vercel read-only hoặc dữ liệu cloud.

## Audit manual run hiện tại

`RunService.start` lấy host `flock` + cooldown, dùng UUID4 run ID, ghi marker local, create metadata QUEUED và tạo asyncio task. Task đi FETCHING → NORMALIZING → ANALYZING → PERSISTING → COMPLETED hoặc PARTIAL; lỗi trước bulk write FAILED, sau khi đã ghi một phần PARTIAL. Cancel/shutdown cố ghi ABORTED; startup recovery dựa marker local để đánh dấu ABORTED. Các write Firestore không thành một transaction toàn run; COMPLETED chỉ sau các write trong luồng hiện tại. Metadata ở `analysis_runs/{id}`, dữ liệu trong subcollections. GET run/list/detail và POST manual không đổi; read-only mode không khởi tạo recovery hoặc worker. Host lock không bảo vệ nhiều máy.

## State machine phiên bản thử nghiệm

Các state cùng tên QUEUED, FETCHING, NORMALIZING, ANALYZING, PERSISTING, COMPLETED, PARTIAL, FAILED, ABORTED có mapper một-một sang legacy; VALIDATING, RETRY_WAIT, CANCELLING, CANCELLED không có tương đương chính xác và mapper từ chối thay vì bịa trạng thái. Mapper không kết nối API.

| From | To |
|---|---|
| QUEUED → FETCHING → NORMALIZING → ANALYZING → PERSISTING → VALIDATING | Bước kế tiếp trong chuỗi |
| Mỗi state chạy | RETRY_WAIT, PARTIAL, FAILED, ABORTED |
| VALIDATING | COMPLETED khi có completion evidence hợp lệ |
| Non-terminal | CANCELLING khi còn owner, hoặc CANCELLED khi không còn owner |
| CANCELLING | CANCELLED sau checkpoint |
| RETRY_WAIT | claim mới về QUEUED sau backoff và còn budget |
| Terminal | Không transition trở lại chạy |

Mỗi mutation tăng revision và ghi audit UTC, state, action và reason code dạng whitelist ký tự; không lưu exception text. COMPLETED đòi expected documents dương bằng persisted documents, validated=true và manifest ID, được lưu trong job. **Evidence chỉ là khai báo của caller**, không chứng minh dữ liệu thật đã lưu; cần transaction/manifest xác minh tại storage trước production. Terminal PARTIAL không tự xóa dữ liệu; ABORTED/FAILED không bị coi là COMPLETED.

## Identity và idempotency

Job ID = hash idempotency key; payload chứa job type, source, scope, window, config version, occurrence ID, tenant scope và có thể thêm field. Payload được canonical JSON băm; cùng key + payload/policy trả cùng job, key tái sử dụng với payload/policy khác báo conflict. Cùng payload/occurrence với key khác trả cùng job trong fake; occurrence khác hoặc config/window khác dùng key mới tạo job khác. Run ID, occurrence ID, job ID, dataset ID và idempotency key có vai trò khác nhau; chưa gắn dataset ID vào Firestore. Retry tăng attempt/fencing trên cùng job. Bản đồ fake chỉ tồn tại trong bộ nhớ: tái tạo client với cùng fake store hoạt động, không chứng minh persistence qua restart process hay bền qua crash.

## Lease, fencing và cancellation

`JobCoordinator` định nghĩa create/get, claim, heartbeat, checkpoint, transition CAS theo owner/token/revision, cancellation, release và publish preflight. Fake dùng `RLock` bao trọn transaction trong một tiến trình; hai luồng thật tranh claim chỉ một luồng thắng. Khi hết hạn, claim mới tăng fencing và attempt, worker cũ bị từ chối heartbeat/transition/release/check_publish. Heartbeat có expiry, interval/lease config và deadline; fake clock UTC. Clock skew, trusted server time, mất mạng, server-side transaction, retry/commit ambiguity và concurrency giữa host chưa được giải quyết.

`check_publish` **chỉ là preflight**, có TOCTOU: publish thực phải kiểm tra fence/revision và đổi active pointer trong cùng transaction với side effect được bảo vệ. Chưa có cơ chế bảo vệ external writes hoặc atomic publish; worker cũ không được phép thực hiện các thao tác này ở production chỉ dựa vào fake. User cancel tăng revision, worker đọc lại, checkpoint thấy CANCELLING và xác nhận CANCELLED; nếu owner hết hạn thì request cancel hoàn tất ngay. Không cắt ngang một external call đang chạy; worker tương lai phải checkpoint trước fetch, giữa batch, trước analysis/persist/publish và xử lý mất lease. Không có worker thực tế trong bước này.

## Retry, recovery, budget

Policy gồm estimate/budget reads/writes/deletes, request budget, retry budget, max attempts, deadline, lease/heartbeat và backoff. Guard Bước 12 chỉ kiểm tra dự toán ban đầu, không đo quota còn lại hay distributed counter. Checkpoint giới hạn số request; retry chờ backoff, dừng khi hết attempts/retry budget/deadline. Crash tại các phase được fake takeover sau expiry về QUEUED; dữ liệu partial không bị xóa, nhưng tính idempotent của ghi dataset thật chưa được chứng minh. Không có transaction retry cloud, network timeout/retry thực tế hoặc cơ chế operator intervention tự động; lỗi và status phải được xử lý khi xây adapter.

## Firestore transaction và blocker

[Firestore transactions](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions) xác nhận read/write atomic và callback có thể chạy lại; [SDK Python transaction](https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.transaction) có giới hạn max_attempts; [contention](https://docs.cloud.google.com/firestore/native/docs/transaction-data-contention) có thể retry hữu hạn rồi lỗi. Đối chiếu 10/11/2026. Chỉ là tài liệu thiết kế; chưa xây adapter, chưa test emulator hay cloud. Không được thay bằng get/update riêng. Cần thiết kế transactional idempotency + lease + revision/fence, retry bounded và xử lý unknown commit, đồng hồ tin cậy/skew, authn/authz owner, fencing tại mọi write/publish, storage manifest/cleanup idempotent, audit retention, quota và adversarial concurrency tests trên emulator trước canary được duyệt. Không tuyên bố multi-host an toàn từ fake.

## Kiểm thử và rollback

- File mới: `Backend/app/jobs/{__init__,contracts,fake}.py`, `Backend/tests/test_job_coordinator.py`, báo cáo này.
- Fake tests: đồng thời claim, expiry/takeover, stale owner, revision CAS, heartbeat, cancellation, retry/backoff/budget, deadline, crash từng stage, completion evidence và replay client với store dùng chung. Không test thực tế SDK transaction retry/mất mạng; không có Firestore emulator/live.
- 143 tests pass, 0 fail/skip; pip check, bash -n, node --check pass; golden 7/7 ở mỗi hash seed 1 và 983; expected không đổi.
- Security: reason code không chứa free-form exception; fake owner/ID không phải xác thực, chưa phù hợp tenant. Chi phí heartbeat/transaction chưa đo; không bật billing.
- Rollback: module chưa được import/wire vào runtime; bỏ commit code/report theo quy trình Git nếu cần, manual-run vẫn hoạt động.
- Bước 13 đạt nghiệm thu contract/fake offline có giới hạn, **live blocked**. Chỉ chuyển Bước 14 với điều kiện chấp nhận blocker và thiết kế publish transaction riêng; không tự bật scheduler/lease/publish.
