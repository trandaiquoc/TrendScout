# Giai đoạn 2 — Bước 3: Ports và Composition Root

**Thời điểm:** 09/11/2026. **Baseline:** `354ce6f`, đã xác minh trùng `origin/main` bằng `git fetch origin main` trước refactor. Phạm vi chỉ ports/DI/composition; không sửa scoring, cleaning, classification, Firestore schema, tạo source, gọi nguồn live, ghi Firestore, thay IAM hoặc deploy.

## 1. Coupling trước/sau

Trước:

```text
FastAPI main → FirestoreStorage + HostRunLock + RunService
                                  RunService → WikimediaAdapter
                                             → clean_snapshot / extract_keywords
                                             → DayData / score_trends → storage
API read-only → import storage_for_request trực tiếp
```

- `RunService` có default trực tiếp khởi tạo `WikimediaAdapter`; import trực tiếp cleaning, keyword và scoring. `create_app` tự khởi tạo Firestore/lock/service. Route read-only tự chọn WIF factory. `SourceAdapter.fetch_day` chưa mô tả `collect`; `CollectionResult` đặt trong Wikimedia adapter.
- Route keyword vẫn chứa filter/search/sort/pagination và validate; startup local dùng host lock/recovery; read-only có xác thực Basic và WIF per request. Các trách nhiệm này không được di chuyển toàn bộ trong bước này.

Sau:

```text
FastAPI main ──→ composition.build_run_service (chỉ local/test)
                      ├─ FirestoreStorage hoặc fake AnalysisStorage
                      ├─ WikimediaAdapter hoặc fake CollectionSource
                      ├─ HostRunLock
                      ├─ clean / keyword / score / DayData factories
                      └─ RunService(source, storage, functions, lock)
API read-only ──→ injected read_storage_factory ──→ composition.read_only_storage
                                                    └─ WIF theo từng request
RunService ──→ CollectionSource / AnalysisStorage / injected callables / models
```

Dependency đi từ composition tới concrete implementations; `RunService` không import adapter Wikimedia, Firebase, HTTP client hoặc implementation cleaning/scoring. Model và source port không import Firebase/HTTP client. AST scan import tĩnh các module `app` không tìm thấy cycle; đây không chứng minh không có mọi dynamic import cycle trong tương lai.

## 2. Port, DI và tương thích

- `CollectionSource` trong `adapters/base.py` mô tả `collect() → CollectionResult`. Result chứa analysis date, snapshots + observations và request count; source/scope, errors và provenance nằm trong models hiện có. `SourceAdapter.fetch_day` được giữ.
- `CollectionResult` chuyển sang `models/observations.py`, và được import lại tại `adapters/wikimedia.py` để đường import cũ `from app.adapters.wikimedia import CollectionResult` vẫn dùng được. Không thay nội dung, scope hay xử lý 404/429.
- `AnalysisStorage` đã có được tái sử dụng; `RunService` nhận storage theo protocol và source factory theo `CollectionSource`; không thay Firestore repository hoặc document/shard format.
- `app/composition.py` lắp adapter thật, storage ADC local, `HostRunLock`, `RunService`, cleaning, extraction, scoring và `DayData`. Test có thể truyền fake source/storage; các function xử lý được inject và chỉ có một implementation nghiệp vụ.
- Ở read-only, `create_app` bỏ qua `build_run_service` trước lifespan recovery; `composition.read_only_storage` chỉ được gọi theo request và dùng WIF hiện có. Không khởi tạo lock/background, không ADC fallback, POST vẫn 405. Basic auth và IAM không đổi.
- Áp dụng Python Protocol, constructor/factory injection và composition root; không đưa framework DI, source mới, scheduler hoặc microservice. Các function use case và `RunService` cũ vẫn tại vị trí cũ; API routing, `start.sh` và Vercel entrypoint được giữ.

## 3. File thay đổi

| File | Thay đổi |
|---|---|
| `Backend/app/composition.py` | Root lắp dependency local và factory đọc WIF. |
| `Backend/app/adapters/base.py` | `CollectionSource` protocol. |
| `Backend/app/models/observations.py` | Chứa `CollectionResult` độc lập adapter. |
| `Backend/app/adapters/wikimedia.py` | Re-export `CollectionResult` từ model cho tương thích import cũ. |
| `Backend/app/orchestration/runs.py` | Nhận source/storage và các hàm xử lý qua constructor. |
| `Backend/app/main.py`, `Backend/app/api/runs.py` | Gọi composition root, inject read-only factory; giữ routes/middleware. |
| `Backend/tests/test_composition.py` | 6 test fake source/storage, failure, cancellation, boundary và read-only. |
| `Documents/Reports/BAO_CAO_BUOC_3_GIAI_DOAN_2.md` | Báo cáo này. |

Các file adapter, Firestore, API, pipeline, dashboard, `setup.sh`, `start.sh`, Vercel config và golden fixtures cũ đều được giữ. `analysis/trend.py` vẫn phụ thuộc Wikimedia source constant; `RunService` vẫn chứa logic run, schema, quota và lock local; router còn chứa filtering. Đây là coupling còn lại dành cho các bước sau, không tuyên bố Clean Architecture hoàn chỉnh.

## 4. Kết quả kiểm thử

- Trước refactor: 7 golden tests pass tại `354ce6f`.
- Sau refactor: full suite **62 pass, 0 fail, 0 skip**: 56 test cũ + 6 test mới. `pip check` pass; `bash -n setup.sh start.sh` và `node --check Frontend/public/app.js` pass.
- Golden 7 test pass với `PYTHONHASHSEED=1` và `983`; serial hóa output đầy đủ hai process bằng nhau, SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, trùng báo cáo Bước 2. `golden_v1/expected.json` không thay đổi.
- Fake source/storage + wrapper processors chứng minh injection và không khởi tạo Wikimedia thật/ADC; fake source failure trả `FAILED`; test cũ bảo vệ `PARTIAL`, write failures và recovery; test mới xác minh cancellation trả `ABORTED`. Local/test kiểm qua ASGI/fake, chưa xác minh start script với Firestore live trong bước này.
- Read-only test ép `VERCEL=1`, xác minh không gọi builder/local Firestore/source, giữ health/405 và đọc run ID cũ `30e96cf96abe48dbb304fb9808c89873` kèm list/detail theo **shard synthetic** trong FakeStorage. Điều này kiểm tra khả năng đọc schema và ID, không đọc hay sửa dữ liệu run live.
- Domain/models/source port được kiểm tra AST không import Firebase/HTTP; kiểm tra orchestration không import concrete adapter/pipeline/storage. Vercel OIDC/WIF chỉ mock, chưa xác nhận quyền cloud/deployment.

## 5. Rủi ro, rollback và điều kiện Bước 4

- `HostRunLock` vẫn chỉ một host; startup local có recovery, không dùng nhiều host như distributed lock. API tìm kiếm vẫn đọc các shard để lọc; Basic auth không phải auth đa tenant.
- Các technical titles có thể lọt cleaning như báo cáo demo; `views_ceil` xấp xỉ, dữ liệu top bị censored. Không sửa logic trong bước này.
- Rollback bằng revert commit refactor và commit báo cáo nếu cần, không rewrite history. Chạy lại golden/full test; dữ liệu đã lưu không cần migration do không đổi schema. Kiểm tra build Vercel và WIF trong môi trường được cấp phép riêng trước khi tuyên bố cloud hoạt động.
- Có thể chuyển Bước 4 sau khi kiểm tra git/remote và duyệt phạm vi source adapter framework; bảo vệ imports tương thích, source identity/coverage, golden, ADC local, WIF read-only. Push lên main có thể kích hoạt Vercel auto build nếu repo được liên kết; phiên này không chủ động deploy.

**Đánh giá:** PASS trong phạm vi offline; cloud live, IAM và production readiness chưa được kiểm chứng.
