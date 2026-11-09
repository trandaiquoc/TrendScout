# Giai đoạn 2 — Bước 5: Repository và migration tương thích

**Ngày:** 09/11/2026. **Baseline code:** `45c6aba`; commit tổ chức báo cáo `de8dba0`. Refactor này không thực hiện migration dữ liệu, gọi nguồn live, ghi/xóa Firestore, deploy hoặc thay IAM/billing.

## 1. Storage trước/sau

Trước: `RunService`, API và infrastructure import `AnalysisStorage`/exceptions từ `app.storage.base`; protocol đã gồm create/get/update run và put/get/list records, không import SDK. Firestore adapter dùng Firebase Admin/Google Cloud và fake dùng dict. API chịu trách nhiệm filter/search/sort/pagination và kiểm tra shard completeness. Composition root chọn Firestore ADC local hoặc WIF read-only.

Sau:

```text
API / RunService → app.ports.storage.AnalysisStorage + StorageError
                               ↑
                 FakeStorage / FirestoreStorage
                               ↑
                    composition root / WIF
```

`app/ports/storage.py` là port ứng dụng, giữ cùng sáu phương thức, `Record`, `RecordKind` và error classes. Protocol runtime-checkable để test structural conformance; kiểm tra này chỉ xác nhận có các thuộc tính, không tự chứng minh chữ ký hay semantic. Contract tests chung kiểm tra hành vi. `app/storage/base.py` re-export cùng objects cũ và giữ `ObservationStorage` legacy; các import cũ hoạt động, exceptions vẫn cùng class. API, orchestration và hai implementation import port trực tiếp. Models/port không import SDK, HTTP hay credentials; Firebase và Google SDK chỉ trong infrastructure/composition wiring.

Không tạo repository/mapper/schema version khác: Firestore hiện đọc/ghi mapping trực tiếp và schema cũ chính là schema hiện hành. Đây là phương án tương thích không biến đổi payload; thiếu/null/zero giữ nguyên. Không chứng minh rằng các schema bất kỳ khác chưa được biết sẽ tương thích.

## 2. Schema và tương thích

Giữ `analysis_runs/{run_id}`, `snapshots/{date}`, `observations/{date}-{chunk}`, `keyword_results/shard-{00..15}`; fields metadata, status, raw, baseline, score, categories, evidence, flags và version không đổi. `baseline_buckets` vẫn là record kind được chấp nhận, không tự tạo dữ liệu. Chưa thêm delete hoặc transaction toàn pipeline; parent document xóa không tự xóa subcollections. `COMPLETED`, partial write/quota guard và recovery thuộc orchestration hiện hữu và được test cũ bảo vệ.

Test dùng ID demo `30e96cf96abe48dbb304fb9808c89873` với **dữ liệu tổng hợp**: metadata, snapshot, observation bucket và keyword shard. Không truy cập run thật. Không sửa search, sort, pagination, HTTP status, auth hoặc scoring. Local vẫn ADC nếu cấu hình; test fake không cần credential; Vercel vẫn OIDC/WIF read-only, không tạo lock/recovery/write pipeline; chưa chứng minh WIF cloud.

## 3. Tổ chức tài liệu

Chuyển năm báo cáo (demo và Bước 1–4) vào `Documents/Reports/`. Sửa link tại README root, hướng dẫn Giai đoạn 2, link README trong báo cáo demo và đường dẫn tự mô tả trong báo cáo Bước 3. Kiểm tra 19 liên kết Markdown nội bộ có target; hướng dẫn quy định báo cáo tiếp theo nằm trong `Documents/Reports/`. Không di chuyển tài liệu thiết kế/hướng dẫn/source.

## 4. File code/test

- Mới: `Backend/app/ports/__init__.py`, `Backend/app/ports/storage.py`, `Backend/tests/test_repository_contract.py`.
- Sửa: `Backend/app/storage/base.py` (compatibility exports), `fake.py`, `firestore.py`; `app/api/runs.py`, `app/main.py`, `app/orchestration/runs.py` (import port). Không đổi Firestore implementation behavior hoặc schema.
- Báo cáo này: `Documents/Reports/BAO_CAO_BUOC_5_GIAI_DOAN_2.md`.

## 5. Kết quả kiểm thử

- **76 pass, 0 fail, 0 skip**: 67 test trước + 9 test contract mới; `pip check`, shell syntax và Node JS syntax pass.
- Bộ contract chung chạy với FakeStorage và FirestoreStorage + SDK client **stub in-memory**, gồm create/read/update, duplicate/missing errors, snapshot, observation bucket, keyword shard, missing/null/0, overwrite, invalid path. Stub còn test document paths, lỗi `PermissionDenied` và tạo lại repository trên cùng client. Stub không phải Firestore Emulator và không chứng minh persistence qua process hoặc cloud.
- Tests cũ/golden bảo vệ API search/sort/pagination/detail/empty, incomplete/partial write, FAILED/ABORTED, chỉ COMPLETED sau ghi, app instance mới dùng cùng fake, read-only không tạo write dependencies. Không có test emulator hay Firestore live trong bước này.
- Golden 7 tests chạy với `PYTHONHASHSEED=1` và `983`, output hai process giống nhau; SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, trùng Bước 2–4. Không sửa `golden_v1/expected.json`.
- Local/test/read-only kiểm qua fake/ASGI và mock. `start.sh` được kiểm tra syntax; Firebase ADC, WIF/IAM, deployment và run live không được kiểm tra lại.

## 6. Rủi ro, rollback và Bước 6

Protocol lưu mapping linh hoạt, chưa có validation schema nghiệp vụ versioned. Router còn đọc toàn bộ keyword shards để lọc. Fake/stub có thể khác SDK thực; index, quota và độ bền Firestore chưa được xác nhận mới. HostRunLock vẫn chỉ một host; một số technical pages có thể còn lọt cleaning; dữ liệu top bị censored và `views_ceil` là xấp xỉ. Các giới hạn này không được sửa ngoài phạm vi Bước 5.

Rollback bằng `git revert` commit refactor và báo cáo nếu cần; commit di chuyển tài liệu có thể revert riêng cùng các tham chiếu. Không rewrite history; dữ liệu cloud không cần rollback vì không được sửa. Trước Bước 6, xác nhận taxonomy/mapping `home_goods`, nhãn `unclassified`, phạm vi và dữ liệu đánh giá; chạy lại golden/contract và giữ các import/schema cũ.

**Đánh giá:** PASS cho repository boundary và tương thích theo test offline; không tuyên bố cloud persistence, emulator hoặc live Firestore được nghiệm thu lại.
