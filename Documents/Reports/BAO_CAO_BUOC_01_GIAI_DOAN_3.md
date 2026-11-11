# TrendScout — Giai đoạn 3, Bước 01: Firestore transactional coordinator

Ngày kiểm tra: 11/11/2026
Baseline: `b3cbe8c` — Phase 2 Step 21; branch `main`; working tree sạch khi bắt đầu.
Kết quả: **`IMPLEMENTED_NOT_EMULATOR_VERIFIED` — `BLOCKED_FOR_LIVE` — `COMMERCIAL_NOT_READY`**.

## Kết luận

Đã bổ sung adapter Firestore transactional riêng cho job coordinator và dataset publication, cùng integration suite dùng client emulator được guard. Chưa thể nghiệm thu transaction thực tế vì Firestore Emulator và Firebase CLI chưa được cài sẵn. Không tải CLI/emulator, không bật kết nối cloud, không dùng ADC/credential, không chạy transaction thực, không deploy và không push. Trạng thái code là `IMPLEMENTED_NOT_EMULATOR_VERIFIED`, không phải `EMULATOR_ACCEPTED`.

Chưa có kết quả transaction, contention, replay hoặc restart từ Emulator; toàn bộ integration suite hiện chưa chạy. Fake vẫn chỉ là reference contract. `BLOCKED_FOR_LIVE` và `COMMERCIAL_NOT_READY` tiếp tục giữ nguyên.

## Audit baseline

- `Backend/app/jobs/contracts.py` định nghĩa `JobCoordinator` port, các state, `Policy`, completion evidence và lỗi coordination.
- `Backend/app/jobs/fake.py` có `FakeCoordinator` dựa trên `threading.RLock`, occurrence/idempotency maps và mutable `OfflineBudget`; code tự ghi rõ không phải distributed lock.
- `Backend/app/datasets/fake.py` có `FakeDatasetStore`, fake resource lease/fence, strict-manifest verify, active-pointer CAS, publication receipts và replay; publication atomically chỉ trong cùng process lock.
- `Backend/app/datasets/manifest.py` xác thực `offline-manifest-v1`; `Backend/app/datasets/evidence.py` dựng lại evidence pipeline và score từ persisted fake shards.
- `Backend/app/workers/refresh.py` là prototype worker dùng coordinator/dataset interfaces; không được nối vào production composition.
- `Backend/app/storage/firestore.py` là Firestore storage CRUD cho demo, khởi tạo Firebase Admin app từ project env/ADC. Đây không phải transaction coordinator, và không được tái dùng cho đường emulator nếu có thể fallback ADC.
- `Backend/app/storage/firestore_emulator.py` cung cấp capability wrapper do factory emulator-only tạo: synthetic project cố định, numeric loopback host, reject ADC/production project env, xác minh namespace chỉ có loopback/no external routes, REST read-only probe và `AnonymousCredentials`. Transaction adapters từ chối client không thuộc wrapper; production composition không import factory.
- `Backend/app/jobs/firestore_transactional.py` triển khai `FirestoreTransactionalCoordinator` và `FirestoreTransactionalDatasetStore`. Tạo job/occurrence/idempotency index, claim/revision/state/budget, resource fencing và heartbeat/retry; prepare shard/report/manifest ngoài transaction; strict verification dựng lại Wikimedia evidence; publish kiểm tra READY, proof/shard inventory, owner/fence, cleanup reservation/tombstone và active-pointer CAS rồi ghi pointer, receipt, prior dataset SUPERSEDED và job COMPLETED trong một transaction. Receipt ổn định theo resource/job giúp replay sau khi pointer đã tiến; `TransactionOutcomeUnknown` buộc caller đối soát durable state. Shard được kiểm tra trước transaction bằng giới hạn JSON bảo thủ 900 KiB để tránh document vượt 1 MiB; caller phải chia payload lớn thành nhiều shard. Adapter không được wire vào production.
- `Backend/emulator_tests/test_firestore_transactional.py` có 16 integration cases cho idempotency/recreated client, cùng occurrence đồng thời, replay sau deadline nhưng chặn create mới, hai-client job/resource claim, SDK callback retry và budget, takeover/stale owner, lost claim acknowledgement, prepare/verify/publish/replay, publish CAS race, cleanup reservation/tombstone, cancellation, budget exhaustion và lost publish acknowledgement. Đây là suite emulator thật khi chạy bằng runner; chưa có test nào trong suite này được thực thi với emulator.
- `Backend/tests/test_firestore_emulator_guard.py` có 10 offline guard tests; `Backend/tests/test_firestore_transactional_model.py` có 4 offline tests cho canonical identity, serialize/restore job/policy/budget và shard payload size/JSON validation.
- `Backend/scripts/run_firestore_emulator_tests.sh` yêu cầu suite, Firebase CLI và JAR cache tồn tại trước; config bind `127.0.0.1`; chạy CLI + emulator + tests cùng `bwrap --unshare-net`; seccomp chỉ cho IPv4/IPv6, từ chối AF_UNIX/socketpair/io_uring; dùng temporary `HOME` không có ADC/login profile, cache JAR read-only, project synthetic và timeout 10 phút. Runner hiện fail-closed code 3 vì Firebase CLI chưa cài; không tải hoặc start emulator.
- `Backend/app/composition.py` giữ wiring API hiện tại; không sửa production composition, manual HostRunLock hoặc Vercel read-only.
- Chỉ có `AGENTS.md` tại root và `Frontend/AGENTS.md`; không có chỉ dẫn Backend riêng.

## Adapter design — implemented, Emulator not verified

Adapter dùng các namespace riêng `_ts3_jobs`, `_ts3_idempotency`, `_ts3_occurrences`, `_ts3_resources`, `_ts3_datasets` và `_ts3_publications`; dữ liệu demo hiện tại không bị migration hoặc sửa. Idempotency/occurrence IDs và resource/receipt IDs là SHA-256 của identity đã chuẩn hóa. Job document lưu policy, budget, revision, state, owner/fence, deadline, identity và 128 audit events gần nhất.

Dataset draft lưu metadata/identity ở document cha, shard trong `shards/{shard_id}`, manifest và quality report trong `meta/manifest`, `meta/quality`. Shard/report/manifest chỉ tạo mới; sau khi seal chuyển `DRAFT → VALIDATING`, adapter không còn đường ghi shard. Verification đọc lại các shard ngoài transaction, chạy strict manifest và persisted Wikimedia evidence verifier, rồi transaction recheck trạng thái/identity/manifest/quality trước `READY`. Publish không scan shard trong transaction: dùng proof gồm shard IDs/digests đã verify, đối chiếu với manifest, rồi transaction đọc job, resource, candidate, manifest, quality, receipt và (khi có) active dataset; nó ghi active pointer, receipt, job `COMPLETED` và dataset supersession atomically. Resource fencing tăng riêng theo resource. Receipt key theo resource/job nên replay cũ không phụ thuộc active pointer hiện thời và không thể rollback pointer mới.

Transaction callbacks chỉ mutate local copies, đọc clock và gọi Firestore transaction API; không fetch source/AI, sleep, sinh identity hoặc side effect RAM dùng chung. SDK retry tối đa 5 attempts theo cấu hình mặc định của adapter. `Aborted` hết retry được phân loại riêng; RPC lỗi sau khi callback hoàn thành trả `TransactionOutcomeUnknown`. Caller cần đọc durable job/receipt trước khi quyết định retry.

Budget giữ một read + write làm recovery reserve. Normal operations không thể tiêu thụ phần này; sau lease expiry, `abort_stale` sử dụng reserved capacity để đánh dấu `ABORTED`, kể cả khi budget thường đã hết. Nếu budget không đủ cho create + một claim + reserve, create fail trước khi ghi. Đây là logical operation accounting; SDK retries, transaction attempts bị hủy và lỗi trước commit có thể phát sinh reads không nằm trong counters đã commit.

| Logical operation | Reads/writes được tính trong budget |
|---|---:|
| Job create mới | 2 / 3 |
| Job claim hoặc state transition | 1 / 1 |
| Resource claim hoặc heartbeat | 2 / 2 |
| Draft / shard / quality / seal | 3/2, 4/2, 4/2, 4/3 |
| Verify với `N` shard documents | `N + 8` / 2 |
| Publish, chưa có active dataset | 6 / 4 |
| Publish thay active dataset | 7 / 5 |
| Receipt replay | 2 / 1 |
| Stale abort reserve | 1 / 1 |

Đây không phải Firestore billing/quota measurements; transaction retries và snapshot/serialization cost có thể làm tổng request lớn hơn. Firestore Emulator không enforce mọi giới hạn document/request production. IAM chưa được kiểm chứng; process có quyền server rộng có thể bypass adapter nếu được cấp quyền ghi/xóa trực tiếp. Vì vậy shard immutability hiện là adapter contract, chưa phải IAM-enforced guarantee.

Pipeline hiện tại phát các partition `scores-v1`, `snapshots-v1`, `raw-observations-v1` dưới dạng một payload mỗi partition. Adapter này chưa được nối vào worker; khi tích hợp cần chia partition lớn thành nhiều shard dưới ngưỡng an toàn 900 KiB và liệt kê đủ shard trong manifest. Guard hiện tại sẽ reject payload quá cỡ trước khi transaction, thay vì chứng minh partitioning đầu-cuối.

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
| Emulator guard/factory | Chỉ loopback, synthetic project, anonymous credentials, không ADC/cloud fallback | Wrapper + 10 offline tests pass; actual REST handshake chưa được xác minh vì emulator vắng mặt. |
| Emulator runner | Suite, cached emulator, synthetic project, loopback, outbound blocked, timeout/nonzero | Có 16 integration cases; runner fail-closed code 3 vì Firebase CLI thiếu; chưa start emulator. |
| Occurrence/idempotent create | Stable identity, replay cùng payload, conflict payload khác, không double budget | SDK transaction code + unit serialization test; emulator concurrency chưa chạy. |
| Job transaction/state | Create/claim/revision/state/cancel/deadline/retry/budget durable | SDK transaction code đã có; còn thiếu emulator proof cho persistence/recovery và deadline matrix. |
| Resource lease/fencing | Token đơn điệu theo resource; takeover; heartbeat/cancel race; stale owner | SDK transactions + two-client claim/takeover/stale-owner cases; heartbeat/takeover interleaving chưa được viết/chạy. |
| Prepare/manifest | Persist shard ngoài transaction; strict READY; immutable candidate | SDK-backed create-only shard path và evidence verifier đã viết; chưa emulator-verified hoặc IAM-verified. |
| Atomic publish | Transaction kiểm tra job/fence/READY/manifest/CAS, ghi pointer + receipt + COMPLETED | Một SDK transaction có kiểm tra bounded metadata và shard proof; chưa được chạy trên emulator. |
| Lost acknowledgement/replay | Commit receipt bền, retry/client restart, không rollback pointer | Transaction receipt/replay code + lost-ack integration cases; chưa thực thi. |
| Retention boundary | Tombstone/reservation/fingerprint chống publish trong cleanup race | Publish từ chối resource reservation/tombstone và dataset reservation/tombstone; cleanup adapter không thuộc scope; chưa test emulator. |
| Failure injection | Callback retry, contention, unknown outcome, budget/cancel/deadline/crash matrix | Có integration cases cho callback retry, races, unknown ack, budget, cancel/takeover và occurrence replay sau deadline; session-loss và một số crash points chưa có emulator case; chưa test nào chạy. |
| API/schema compatibility | Giữ API demo, Firestore schema, HostRunLock, Vercel read-only, golden | Baseline không đổi; full regression pass. |
| Security/no-cloud | Không credentials/cloud; network chỉ loopback trong runner | No cloud calls được thực hiện; bubblewrap network namespace đã được kiểm tra. |
| Scheduler/cleanup | Production worker, scheduler và live cleanup vẫn disabled | Giữ nguyên trạng thái disabled theo baseline; không thay composition. |

Không có nhóm nào trong acceptance matrix được xem là nghiệm thu emulator. Tất cả các invariant bắt buộc cho `EMULATOR_ACCEPTED` còn chưa được chứng minh.

## Verification đã chạy

Lệnh `./Backend/scripts/verify_offline_readiness.sh` chạy trong strict seccomp sandbox, chặn socket Internet:

- 371/371 unittest pass, gồm 10 guard tests và 4 model tests. Emulator integration suite được cố ý tách khỏi default discovery và chưa chạy.
- Golden Regression: 7/7 với `PYTHONHASHSEED=1` và 7/7 với `983`.
- `pip check`: không có dependency conflict.
- `bash -n`, `node --check`, Vercel asset bundle byte comparison: pass.
- Chrome dashboard, keyboard/mobile và adapter → ASGI API → dashboard E2E với fixture synthetic: pass.
- Synthetic API/storage benchmark 1k/5k/10k: pass.
- Dòng kết quả: `OFFLINE_READINESS_VERIFICATION=PASS`.
- Emulator guard chạy trực tiếp bên trong bubblewrap; do port trống, factory trả lỗi unavailable trước khi khởi tạo SDK client. Emulator runner preflight trả code 3 vì Firebase CLI thiếu, trước khi tạo temp config hoặc start emulator.
- Emulator network probe: `loopback PASS; public route BLOCKED; AF_UNIX EPERM`, filter kế thừa ở process con.
- Adapter và 16 emulator test cases qua `py_compile`; guard/model tests pass. Chạy runner trả code 3: `Firebase CLI is not installed; no downloads were attempted.`

Đây là regression baseline offline, không phải emulator acceptance. Không có integration test nào bị skip rồi báo pass; không thay Golden Expected. Không gọi Wikimedia, Firestore Cloud, Firebase Console, gcloud cloud API, AI, marketplace hoặc deployment. Không đọc ADC/credential và không tạo document. Emulator integration suite chưa được chạy.

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

Tiến độ local gồm các commit trước đó (`8373d5a test(firestore): add isolated emulator test guard`, `9c22d7d docs: update phase 3 step 1 emulator blocker`) và commit code `cecd1f3 feat(firestore): add emulator transaction adapter`. Báo cáo này được commit riêng với trạng thái chưa emulator-verified. Không push. Các commit này ghi nhận tiến độ, không phải nghiệm thu. Sau khi CLI/JAR được cài riêng và xác minh trong cache, tiếp tục runner, sửa lỗi theo bằng chứng, full failure matrix và hai commit nghiệm thu đúng message. `EMULATOR_ACCEPTED` chỉ được đặt nếu mọi điều kiện nghiệm thu thực sự pass.
