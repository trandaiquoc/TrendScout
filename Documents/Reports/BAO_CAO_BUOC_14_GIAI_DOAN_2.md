# Bước 14 — Scheduler và publish (offline)

Trạng thái: **OFFLINE PROTOTYPE IMPLEMENTED; BLOCKED_FOR_LIVE; nghiệm thu offline đầy đủ còn thiếu**. Không chạy lịch thực, worker thực, Wikimedia hay Firestore cloud; không thay API, manual-run, HostRunLock, Vercel hoặc schema demo. Policy lịch/quality/fallback là minh họa PENDING_APPROVAL; refresh không đồng nghĩa retention.

## Kiến trúc và chính sách

`Schedule` tính mốc UTC từ anchor cố định, interval mặc định 7 ngày, occurrence hash theo schedule ID/version/thời điểm, không trôi theo thời gian job chạy. `FakeScheduler` phải được gọi chủ động, mặc định disabled, hỗ trợ pause/resume/stop, cooldown, giới hạn concurrent và budget do caller cung cấp. Missed policy chỉ chọn kỳ gần nhất, tối đa 1 catch-up, không chạy dồn. Bộ nhớ/lock chỉ trong process; không phải scheduler bền vững hoặc multi-host. Retry/deadline nằm ở job policy Bước 13; chưa nối dispatch với job creation tự động, không có cron.

`RefreshWorker` prototype nhận coordinator, dataset store và các hàm fetch/clean/analyze được inject. Nó claim job/resource, checkpoint, chuyển state, tạo draft, seal/verify rồi publish. Unit test chỉ inject hàm synthetic; **chưa nối source registry/Wikimedia, cleaning, classification, algorithm engine hoặc storage port thật**, chưa có batch-fetch checkpoints, heartbeat trong lúc external call dài, retry tự động, budget từng storage write hay khôi phục draft sau crash. Failure được ghi FAILED nếu còn lease; cancellation cooperative; external call đang chạy không thể hủy ngay. Các phần này cần hoàn thiện/test trước nghiệm thu offline đầy đủ.

## Resource fencing và atomic publish fake

Bước 13 fencing tăng theo job, do vậy không đủ phân biệt hai job cùng stream. `FakeDatasetStore` dùng resource lease riêng với token tăng theo resource, cùng RLock với coordinator. Hai job cùng resource tranh claim chỉ một thành công; sau expiry job khác token cao hơn. Trong cùng khóa, publish kiểm job owner/job fence/revision/expiry/deadline, resource owner/token/expiry, state VALIDATING, dataset READY/verification fingerprint và CAS active pointer; chuyển pointer, dataset và job COMPLETED cùng một critical section. Fake chưa chứng minh crash atomicity hoặc đa host, không dùng làm distributed lock.

Sau publish thành công, retry cùng publication nhận ALREADY_PUBLISHED, không thay active pointer kể cả khi pointer đã thay đổi. Test mô phỏng mất acknowledgement bằng gọi lại sau commit, không mô phỏng network/commit ambiguity thực. Dataset cũ chỉ chuyển SUPERSEDED và không bị xóa; refresh fail giữ active cũ. Chưa triển khai hiển thị STALE hoặc policy retention vì chưa được duyệt.

## Draft, manifest, quality

Draft namespace theo dataset ID, owner/resource token; shard đã ghi không cho thay giá trị, sau seal không cho thêm. Manifest lưu map shard ID → SHA-256 canonical JSON; verification đọc lại fake stored shards và so key/digest, kiểm nguồn metadata và tối thiểu một item đã lưu, đồng thời yêu cầu quality flag. Manifest/metadata/counts băm thành fingerprint; publish đòi fingerprint còn đúng và READY. Thiếu shard/digest sai/quality fail trở thành REJECTED, không publish. Test caller boolean không đủ nếu dữ liệu rỗng hoặc thiếu metadata. Đây chỉ là quality gate tối thiểu synthetic, **chưa có schema đầy đủ cho source/scope/window/version/count/provenance, required partitions hoặc kiểm tra chất lượng Wikimedia thực**. Bulk writes và verify trong fake dùng cùng lock để đơn giản; production phải tách verify tốn kém khỏi transaction ngắn và bảo vệ immutable versions.

## Firestore và blockers

[Firestore transactions](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions) yêu cầu reads trước writes, callback có thể được chạy lại và commit atomically; [Python SDK transaction](https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.transaction) có max_attempts. [Giới hạn](https://cloud.google.com/firestore/quotas) và chi phí cần kiểm tra lại khi thiết kế. Đối chiếu 10/11/2026. Chưa tạo Firestore transactional adapter, emulator không có sẵn, không cài thêm; chưa test cloud. Cần transaction thực kiểm job/resource fence + active revision + manifest version + job state, xử lý commit ambiguity, giới hạn size/read, trusted time/clock skew, claim/release và heartbeat, authentication, cancellation race, idempotency bền vững, kiểm thử emulator và multi-host. Không gọi API bên ngoài trong callback. Lease resource fake chưa tự gia hạn khi heartbeat job; worker phải dừng an toàn khi resource hết hạn.

Quota estimates Bước 12 vẫn chỉ là dự toán; chưa có distributed quota counter. Scheduler/worker ngoài HTTP như scheduler và worker được quản lý hoặc host Ubuntu luôn bật là lựa chọn cần đánh giá quota, concurrency, vận hành và chi phí trước phê duyệt; Vercel read-only không chạy worker. Không thay billing/IAM/credentials.

## Kiểm thử và những phần còn thiếu

- Kiểm tra UTC 7 ngày, missed/latest-only, pause/stop/cooldown, budget/concurrency input; test resource fence qua job khác nhau, claim hai luồng, CAS, concurrent publish, stale owner, lost-ack replay, missing shard/digest sai/quality fail, draft immutable, cancellation checkpoint và lỗi fetch giữ active cũ.
- 152 tests pass, 0 fail/skip; golden 7/7 với mỗi seed 1 và 983, golden expected giữ nguyên. `pip check`, `bash -n setup.sh start.sh`, `node --check Frontend/public/app.js` pass.
- Chưa kiểm thử đầy đủ 404/429/algorithm skipped, retry/backoff end-to-end, quota hết giữa persist, partial writes/restart/resume draft, crash trước publish, server transaction failure hoặc lost network; chưa chứng minh restart process giữ fake state. Không gọi fake test là emulator/live.
- File: `Backend/app/scheduling/recurrence.py`, `Backend/app/datasets/fake.py`, `Backend/app/workers/refresh.py`, các `__init__.py`, `Backend/tests/test_refresh_offline.py`, báo cáo này.
- Rollback: module chưa wire vào app; revert commit code/report theo quy trình Git, manual-run nguyên trạng. Điều kiện sang Bước 15: hoàn thiện và nghiệm thu offline những thiếu sót nêu trên, thống nhất retention/fallback; live vẫn blocked cho đến khi adapter và an toàn multi-host được chứng minh và phê duyệt.

## Bổ sung nghiệm thu offline — vòng 2

**OFFLINE_INCOMPLETE; BLOCKED_FOR_LIVE.** Đã xử lý một phần blocker cũ: worker chạy business components thật với fake source, scheduler nối được job và có mô phỏng recovery một trường hợp. Chưa hoàn thiện manifest, retry và toàn bộ ma trận recovery nên không đạt nghiệm thu offline đầy đủ.

### Pipeline và dispatch

`OfflineWikimediaPipeline` chỉ chấp nhận `httpx.MockTransport`: SourceRegistry → WikimediaAdapter thực (từng ngày fixture) → `clean_snapshot` Bước 7 → extraction/classification v0.1 → `algorithm_engine().legacy_results` / scoring v0.1 một lần. Output score shard được fake persist, đọc lại, verify digest và publish CAS. Test dùng `golden_v1/source.json` synthetic, so từng entity với `golden_v1/expected.json`, gồm score null; expected không sửa. Scheduler occurrence tạo job qua `dispatch_job`; coordinator fake store dùng lại cho client mới cho cùng job ID, key trùng payload khác báo conflict. Đây là state trong RAM, không phải process restart hoặc storage bền.

### Checkpoint, heartbeat và recovery

Thêm checkpoints trước/sau fetch, cleaning, analysis; trước/sau draft, trước từng shard, seal, verify, publish. Mỗi checkpoint kiểm tra job revision/cancel, resource fencing và gia hạn job/resource trong cùng fake lock; test expiry tại 13 điểm không publish. Không thể heartbeat trong sync blocking call; nếu lease hết hạn, bước sau từ chối publish. Adapter fixture gọi từng ngày nhưng chưa có checkpoint giữa các request trong fetch. Cancel được kiểm tra giữa các bước; không hủy được sync call đang thực hiện.

Fault injection crash bằng `BaseException` trước seal sau shard write: giữ draft cũ; sau expiry, coordinator mới cùng fake state và dataset repository còn trong RAM, worker claim token mới và `fresh_on_retry=True` tạo namespace mới, publish đúng một lần. Các checkpoint khác mới test **lease expiry**, không phải kiểm thử crash/recovery từng điểm; chưa resume draft verified hay tránh fetch lại. Chưa có fault injection sau publish trước acknowledgement ở worker; fake publication replay đã test ở vòng trước. Không xóa draft. Đường retry tự động, phân loại transient/permanent, backoff/timeout và quota mỗi write chưa triển khai; 404/429/500 ngày phân tích hiện đi FAILED, không tự retry, không biến thành 0. Test không có active mới sau các lỗi này; test cũ xác nhận failure giữ active trước đó. Scheduler fake đánh dấu dispatched trước job create, nếu create lỗi cần recovery/atomic dispatch: chưa chứng minh crash an toàn tại ranh giới này.

### Manifest và publication

Giữ manifest map shard/digest, kiểm nội dung persisted, metadata nguồn, count tối thiểu, quality flag, fingerprint bất biến và READY; bảo vệ resource fence và active CAS bằng fake lock. Chưa có contract đầy đủ dataset/job/occurrence/source/scope/window/version/partition/count/provenance/quality report; chưa phân biệt tốt partition rỗng hợp lệ, chưa bảo đảm quality cho missing snapshot/baseline theo policy được duyệt. Không khẳng định các dataset partial được ngăn theo mọi policy. Cần hoàn thiện manifest/quality gate và test tampering, missing/extra partitions trước nghiệm thu. Resource fencing giữa job khác occurrence, stale owner và fake publication replay tiếp tục được test, không suy ra Firestore atomicity.

### Kiểm thử, giới hạn và bước tiếp theo

- 157 tests pass, 0 fail/skip; golden tests 7/7 cho mỗi seed 1 và 983; pip check, bash -n và node --check pass. Integration test chạy pipeline thật bằng HTTP mock, không gọi cloud/network.
- Môi trường không có `firebase` CLI hoặc `FIRESTORE_EMULATOR_HOST`; chưa chạy emulator và chưa có transactional Firestore publisher. Không bật scheduler/worker, runtime cache, classifier mới, AI, retention hoặc billing; API/schema/Vercel/manual-run giữ nguyên.
- Trước khi chốt OFFLINE_ACCEPTED: hoàn thiện manifest/version/quality dựa persisted records; recovery và retry bounded theo failure matrix, dispatch không mất occurrence khi job create lỗi, batch checkpoint, quota từng write, lost ack và full crash matrix; chạy test hợp đồng. Cần duyệt policy quality/partial, scheduler, retention/fallback và budget (`PENDING_APPROVAL`). Bước 15 chưa bắt đầu.
- LIVE: cần adapter transaction atomic cùng lease/resource fence/manifest revision/pointer, đồng hồ tin cậy, emulator hai client, xử lý unknown commit, IAM/hosting/quota/rollback được duyệt và bằng chứng phù hợp. Fake state không chứng minh multi-host safety.
