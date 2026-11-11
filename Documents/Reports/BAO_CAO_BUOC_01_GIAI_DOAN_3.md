# TrendScout — Giai đoạn 3, Bước 01: Firestore transactional coordinator

Ngày kiểm tra: 11/11/2026
Baseline: `b3cbe8c` — Phase 2 Step 21; branch `main`; working tree sạch khi bắt đầu.
Kết quả: **`EMULATOR_NOT_VERIFIED` — `BLOCKED_FOR_LIVE` — `COMMERCIAL_NOT_READY`**.

## Kết luận

Không thể nghiệm thu Firestore transaction ở goal này vì Firestore Emulator chưa được cài và không có sẵn trong môi trường. Tôi không tải CLI/emulator, không bật kết nối cloud, không dùng ADC/credential, không chạy transaction, không deploy và không push. Phần kiểm chứng transaction phụ thuộc emulator dừng tại prerequisite cài emulator; trong lúc đó đã bổ sung guard/runner và regression tests offline mà không khẳng định transaction đã được triển khai.

Chưa có transactional job coordinator, resource lease/fencing hoặc atomic dataset publisher mới; do đó chưa có kết quả concurrency/replay từ Firestore SDK hoặc emulator và không được gọi là `IMPLEMENTED_NOT_EMULATOR_VERIFIED`. Chưa đạt `EMULATOR_ACCEPTED`. Các fake hiện tại vẫn chỉ là reference contract, không chứng minh distributed safety. Sau audit, đã thêm riêng một emulator-only client guard cùng 9 offline tests; guard này chưa kết nối emulator thật.

## Audit baseline

- `Backend/app/jobs/contracts.py` định nghĩa `JobCoordinator` port, các state, `Policy`, completion evidence và lỗi coordination.
- `Backend/app/jobs/fake.py` có `FakeCoordinator` dựa trên `threading.RLock`, occurrence/idempotency maps và mutable `OfflineBudget`; code tự ghi rõ không phải distributed lock.
- `Backend/app/datasets/fake.py` có `FakeDatasetStore`, fake resource lease/fence, strict-manifest verify, active-pointer CAS, publication receipts và replay; publication atomically chỉ trong cùng process lock.
- `Backend/app/datasets/manifest.py` xác thực `offline-manifest-v1`; `Backend/app/datasets/evidence.py` dựng lại evidence pipeline và score từ persisted fake shards.
- `Backend/app/workers/refresh.py` là prototype worker dùng coordinator/dataset interfaces; không được nối vào production composition.
- `Backend/app/storage/firestore.py` là Firestore storage CRUD cho demo, khởi tạo Firebase Admin app từ project env/ADC. Đây không phải transaction coordinator, và không được tái dùng cho đường emulator nếu có thể fallback ADC.
- `Backend/app/storage/firestore_emulator.py` là factory emulator-only mới: synthetic project cố định, numeric loopback host, reject ADC/production project env, kiểm tra network namespace chỉ có loopback/no external routes và REST read-only probe trước khi tạo `google.cloud.firestore.Client` với `AnonymousCredentials`. Factory không được gọi từ production composition.
- `Backend/tests/test_firestore_emulator_guard.py` có 9 tests offline cho thiếu host, endpoint ngoài loopback, production project/ADC, namespace host, emulator unavailable, REST response sai schema và explicit anonymous client construction.
- `Backend/scripts/run_firestore_emulator_tests.sh` là runner chuẩn bị sẵn: yêu cầu integration test files, Firebase CLI và JAR cache tồn tại trước; config tạm bind `127.0.0.1`; chạy CLI + emulator + tests trong cùng `bwrap --unshare-net`; seccomp chỉ cho IPv4/IPv6, từ chối AF_UNIX/socketpair/io_uring; dùng temporary `HOME` không có ADC/login profile, bind cache JAR read-only, project synthetic và timeout 10 phút. Hiện runner fail-closed code 4 do chưa có integration test suite; nó không tải hoặc start gì.
- `Backend/app/composition.py` giữ wiring API hiện tại; không sửa production composition, manual HostRunLock hoặc Vercel read-only.
- Chỉ có `AGENTS.md` tại root và `Frontend/AGENTS.md`; không có chỉ dẫn Backend riêng.

## Environment và isolation

Đã kiểm tra mà không khởi tạo Firestore client:

| Kiểm tra | Kết quả |
|---|---|
| Python Firestore SDK | Có, `google-cloud-firestore 2.34.1` trong `Backend/.venv`. |
| Java | `21.0.12`. |
| Firebase CLI | Không có trong `PATH`. |
| Firestore Emulator JAR | Không tìm thấy trong cache hoặc các vị trí cài đặt đã rà soát. |
| Firestore emulator process/listener | Không có process; không có listener tại `127.0.0.1:8080`. |
| Google Cloud CLI | Có, Snap `Google Cloud SDK 592.0.0`; component list local không có `cloud-firestore-emulator`. Snap wrapper bị từ chối bởi capability policy, nhưng executable bên trong Snap chạy được cho thao tác inventory local. |
| CLI Java compatibility | Release notes hiện hành cho SDK `592.0.0` yêu cầu Java 25 cho Firestore Emulator; máy hiện có Java 21. |
| Bubblewrap + seccomp isolation | `bwrap --unshare-net` hoạt động. Probe kết hợp seccomp pass: loopback TCP round-trip thành công; public `1.1.1.1:443` không có route; AF_UNIX và socketpair trả `EPERM`, kể cả process con. Factory xác nhận `/proc/net/dev` và route state chỉ có loopback; Unix socket proxy không được phép trong emulator process tree. |

Bubblewrap tạo network namespace riêng cho command tree và vẫn cho loopback; vì vậy emulator và test runner phải được khởi chạy **cùng trong một namespace**. Chạy emulator trên host rồi chạy test trong namespace khác sẽ không kết nối được. Lệnh `unshare -n` trực tiếp bị `Operation not permitted`, nhưng bubblewrap được xác minh hoạt động.

## Tài liệu chính thức đã đối chiếu

Truy cập ngày 11/11/2026:

- [Firestore transactions and batched writes](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions): mọi transaction read phải trước write; callback có thể chạy lại khi contention; transaction commit atomic và callback không nên có side effect ứng dụng.
- [Firestore transaction contention](https://docs.cloud.google.com/firestore/native/docs/transaction-data-contention): SDK server-side dùng built-in transactions; production concurrency mode thay đổi locking behavior; emulator không thay được cloud evidence.
- [Firestore Emulator limitations](https://docs.cloud.google.com/firestore/native/docs/emulator): emulator dùng cơ chế locking đơn giản, không mô phỏng concurrency modes hoặc mọi giới hạn production. Emulator pass không chứng minh multi-host production safety.
- [Firestore Python `Transaction` reference](https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.transaction.Transaction): SDK cài đặt là 2.34.1; Transaction hỗ trợ `max_attempts` (mặc định 5) và API transaction.
- [Google Cloud CLI component manager](https://docs.cloud.google.com/sdk/docs/components): Cloud Firestore Emulator là component riêng và tài liệu liệt kê cách cài component; môi trường hiện tại là Snap và component không được cài.
- [Google Cloud CLI 592 Firestore emulator command](https://docs.cloud.google.com/sdk/gcloud/reference/emulators/firestore/start) và [release notes](https://docs.cloud.google.com/sdk/docs/release-notes): emulator mặc định bind loopback; CLI 592 yêu cầu Java 25.
- [Firebase Emulator setup](https://firebase.google.com/docs/emulator-suite/install_and_configure): Firebase CLI tải emulator binaries vào `~/.cache/firebase/emulators/` khi chưa có.
- [Firebase Firestore emulator install command](https://firebase.google.com/docs/firestore/security/test-rules-emulator): `firebase setup:emulators:firestore` cài emulator Firestore.
- [Connecting Admin SDK to emulator](https://firebase.google.com/docs/emulator-suite/connect_firestore): Admin SDK dùng `FIRESTORE_EMULATOR_HOST` (không có scheme) và project ID phải khớp project emulator.
- [Firestore quotas and limits](https://docs.cloud.google.com/firestore/quotas): production có giới hạn document 1 MiB và API request 10 MiB; emulator không enforce mọi giới hạn này.

## Acceptance matrix

| Nhóm | Yêu cầu | Kết quả hiện tại |
|---|---|---|
| Emulator guard/factory | Chỉ loopback, synthetic project, anonymous credentials, không ADC/cloud fallback | Guard + 9 mocked offline tests pass; actual emulator REST probe chưa được chạy vì emulator binary/listener vắng mặt. |
| Emulator runner | Có test suite, cached emulator, synthetic project, loopback, outbound blocked, timeout/nonzero | Runner syntax pass; chạy preflight trả code 4 `no Firestore integration test suite is present`; chưa start Firebase CLI/emulator. |
| Occurrence/idempotent create | Stable identity, replay cùng payload, conflict payload khác, không double budget | Fake có reference behavior; Firestore chưa có. |
| Job transaction/state | Create/claim/revision/state/cancel/deadline/retry/budget durable | Chưa triển khai hoặc kiểm chứng. |
| Resource lease/fencing | Token đơn điệu theo resource; takeover; heartbeat/cancel race; stale owner | Chỉ fake lock test; hai SDK client chưa được chạy. |
| Prepare/manifest | Persist shard ngoài transaction; strict READY; immutable candidate | Fake implementation có; Firestore backing/immutability chưa có. |
| Atomic publish | Transaction kiểm tra job/fence/READY/manifest/CAS và ghi pointer + receipt + COMPLETED | Chỉ fake atomic lock; chưa có SDK transaction. |
| Lost acknowledgement/replay | Commit receipt bền, retry và client restart, không rollback pointer | Fake-only; emulator chưa chạy. |
| Retention boundary | Tombstone/reservation/fingerprint chống publish trong cleanup race | Fake-only; chưa có transaction boundary thực. |
| Failure injection | Callback retry, contention, unknown outcome, budget/cancel/deadline/crash matrix | Fake tests tồn tại; SDK/emulator matrix chưa chạy. |
| API/schema compatibility | Giữ API demo, Firestore schema, HostRunLock, Vercel read-only, golden | Baseline không đổi; full regression pass. |
| Security/no-cloud | Không credentials/cloud; network chỉ loopback trong runner | No cloud calls được thực hiện; bubblewrap network namespace đã được kiểm tra. |
| Scheduler/cleanup | Production worker, scheduler và live cleanup vẫn disabled | Giữ nguyên trạng thái disabled theo baseline; không thay composition. |

Không có nhóm nào trong acceptance matrix được xem là nghiệm thu emulator. Tất cả các invariant bắt buộc cho `EMULATOR_ACCEPTED` còn chưa được chứng minh.

## Verification đã chạy

Lệnh `./Backend/scripts/verify_offline_readiness.sh` chạy trong strict seccomp sandbox, chặn socket Internet:

- 366/366 unittest pass, gồm 9 test guard mới; emulator tests không bị skip vì chưa có emulator suite nào được thêm vào default unittest discovery.
- Golden Regression: 7/7 với `PYTHONHASHSEED=1` và 7/7 với `983`.
- `pip check`: không có dependency conflict.
- `bash -n`, `node --check`, Vercel asset bundle byte comparison: pass.
- Chrome dashboard, keyboard/mobile và adapter → ASGI API → dashboard E2E với fixture synthetic: pass.
- Synthetic API/storage benchmark 1k/5k/10k: pass.
- Dòng kết quả: `OFFLINE_READINESS_VERIFICATION=PASS`.
- Emulator guard chạy trực tiếp bên trong bubblewrap; do port trống, factory trả lỗi unavailable trước khi khởi tạo SDK client. Emulator runner được kiểm tra riêng và fail-closed khi chưa có suite.
- Emulator network probe: `loopback PASS; public route BLOCKED; AF_UNIX EPERM`, filter kế thừa ở process con.

Đây là regression baseline offline, không có emulator tests. Không có test nào bị đổi thành pass giả; không thay Golden Expected. Không gọi Wikimedia, Firestore, Firebase Console, gcloud cloud API, AI, marketplace hoặc deployment. Không đọc ADC hoặc credential. Không tạo document.

## Prerequisite để tiếp tục

Máy đã có Node.js 24 và Java 21. Cài Firebase CLI và tải đúng emulator binary cần network; tôi không chạy các lệnh này theo giới hạn hiện tại:

```bash
npm install --global firebase-tools
firebase setup:emulators:firestore
```

Không cần `firebase login`. Khi cài xong, tiếp tục dùng **chỉ** project ID synthetic `demo-trendscout-p3s1`, endpoint `127.0.0.1:8080`, và chạy emulator cùng test bên trong `bwrap --unshare-net`. Firebase CLI docs cho biết emulator cache nằm ở `~/.cache/firebase/emulators/`; runner phải kiểm tra binary cache tồn tại trước khi start và fail nonzero nếu không có, không tự tải.

Không khuyến nghị start gcloud SDK 592 emulator trên máy này nếu chưa nâng cấp Java: tài liệu version hiện tại yêu cầu Java 25. Cài Java hoặc CLI/emulator là thay đổi môi trường hệ thống và không thuộc hành động đã thực hiện.

## Decisions requiring owner approval

Các mục dưới đây tiếp tục **`PENDING_APPROVAL`**; không được suy ra từ code hoặc emulator:

- Hosting/scheduler/worker runtime và production rollout.
- IAM roles, ADC/WIF và quyền service principal.
- Tenant authentication, server-side RBAC.
- Retention, delete access, grace period và legal hold.
- Quota/billing/cost cap.
- Backup/restore, RPO/RTO.
- Monitoring, on-call, SLO.
- Data/source permissions và license.

## Git và giới hạn nghiệm thu

Tiến độ local được ghi thành hai commit: `8373d5a test(firestore): add isolated emulator test guard` và commit báo cáo này. Đây không phải completion report. Không chạy cloud, không deploy và không push. Sau khi emulator binary được cài riêng và được xác minh trong cache, tiếp tục triển khai coordinator/publisher, integration tests, failure matrix và các commit trạng thái trung thực; `EMULATOR_ACCEPTED` chỉ được đặt nếu mọi điều kiện nghiệm thu thực sự pass.
