# Bước 12 — Storage optimization và quota (offline)

Đo offline, mô hình dự toán v1, guard và contract identity thử nghiệm; không đổi runtime, Firestore schema, cache, index hoặc cloud. Không kết luận quota Spark còn bao nhiêu; usage, billing và storage thực tế chưa đo.

## Storage và operation

`analysis_runs/{run_id}` chứa metadata; subcollection gồm `snapshots`, `observations`, `keyword_results`, `baseline_buckets` theo port, nhưng pipeline hiện không ghi baseline_buckets. Create ghi một document; các lần update trạng thái và recovery ghi riêng; `_execute` cuối cùng đọc metadata. Mỗi snapshot ghi một document; observation chunk 40 keywords/document; keyword results 16 shards theo ký tự đầu ID. `safe_doc` chặn JSON >700.000 bytes, không phải kích thước Firestore chính xác. Guard dự kiến 1.000 writes/run và host lock/cooldown hiện có không phải quota guard phân tán hay số quota còn lại.

GET metadata: một document. GET keywords: một metadata + stream toàn bộ shards rồi lọc/sort/page bằng Python: 16 shard là 17 reads ước tính mỗi lần, kể cả search/filter/offset; limit=50 không giảm số reads. GET detail: metadata + một shard, ước tính 2 reads. Query rỗng có minimum charge; retry/index reads có thể thêm. PARTIAL/FAILED tùy giai đoạn lỗi; COMPLETED chỉ sau khi ghi đủ nhưng không có transaction toàn run. Parent deletion không xóa subcollections. Sau restart dữ liệu đọc lại từ Firestore khi sẵn sàng; test cũ xác nhận qua fake. Vercel read-only tạo identity client theo request, instance/cold start riêng; không có cache chia sẻ.

List stream collection không lọc field; detail lấy document ID trực tiếp; chưa chứng minh cần composite index. Map/array lớn có thể tạo overhead; chỉ đề xuất kiểm tra index exemption các field payload không query sau audit và test, không đổi index cloud. JSON/chunk không đảm bảo chính xác Firestore encoding, index entry, storage hoặc network egress. Object storage có thể hữu ích cho raw lớn nếu license cho phép; PostgreSQL có thể hỗ trợ query/pagination/transaction, cần benchmark và đánh giá vận hành riêng.

## Quota và mô hình

Đối chiếu 10/11/2026: [Firestore quotas](https://firebase.google.com/docs/firestore/quotas) nêu free tier 50.000 reads/ngày, 20.000 writes/ngày, 20.000 deletes/ngày, 1 GiB stored, 10 GiB outbound/tháng; reset khoảng nửa đêm giờ Pacific, chỉ một free database/project. Document tối đa 1 MiB, 40.000 index entries/document và 8 MiB tổng index entries. [Pricing](https://firebase.google.com/docs/firestore/pricing) nêu minimum một read/query rỗng, index reads và storage overhead; [storage size](https://firebase.google.com/docs/firestore/storage-size) giải thích encoding. Không bật billing; xác minh lại trước vận hành.

Mô hình `storage-estimate-v1`: writes = runs × 232; content docs = runs × 226; JSON bytes = runs × 9.412.755; reads = list × (1 + max(1, shards)) + detail × 2 + metadata × 1. Các giá trị/run từ demo lịch sử, không cố định, chưa gồm retries, failure, index, users khác hay dung lượng đã có. Deletes=0 vì không có cleanup; cleanup tương lai cần tính document xóa và retries. JSON bytes không phải cloud egress hay storage.

| Kịch bản độc lập | Writes ước tính | JSON thêm |
|---|---:|---:|
| 1 run/7 ngày | 232/tuần | 9,41 MB/tuần |
| 1 run/ngày | 232/ngày | 9,41 MB/ngày |
| 10 run/ngày | 2.320/ngày | 94,13 MB/ngày |
| 100 run/ngày | 23.200/ngày | 941,28 MB/ngày |

100 run/ngày vượt 20.000 writes trước retry; 1 run/ngày trong 30 ngày thêm 282,38 MB JSON, 10/ngày thêm 2,82 GB JSON/30 ngày, chưa overhead. Giả định mỗi viewer gọi list/search/filter/pagination 10 lần/ngày: 10 viewers ≈1.700 reads/ngày; 100 ≈17.000; 1.000 ≈170.000, chưa metadata riêng, detail, retry, index. Dừng khi dự toán cộng usage đã đo đáng tin vượt budget an toàn hoặc không xác minh được điều kiện; không tự nâng plan. Usage console không nhất thiết tức thời.

## Benchmark và cache

`CountingStorage` là decorator opt-in cho AnalysisStorage, chưa nối runtime. Nó đếm calls/success/failure, documents fake trả về, reads/writes ước tính, byte JSON và latency; không log nội dung/ID/credential. Failure có thể phát sinh cloud charge hoặc partial write chưa biết; SDK retries không quan sát được. Port không có delete; không đo delete thực, index reads, egress hay storage. `check_budget` offline nhận budget và estimate rõ ràng, báo reason khi vượt; không phải counter hay bảo vệ concurrent hosts. Identity contract từ chối chia sẻ nếu chưa cho phép, không tự xác thực license.

`cd Backend && PYTHONPATH=. .venv/bin/python scripts/benchmark_storage_offline.py`: 10 request ASGI qua endpoint, fake storage và 16 shard, search/filter/pagination, limit 50. Dữ liệu tổng hợp, padding 300 ký tự/entity; latency và tracemalloc tùy máy, JSON bytes không phải wire Firestore.

| Entity | Requests | Reads dự toán | JSON storage response | HTTP response | elapsed | Peak traced |
|---:|---:|---:|---:|---:|---:|---:|
| 1.000 | 10 | 170 | 5.022.800 B | 251.259 B | 0,2173 s | 1.617.484 B |
| 5.000 | 10 | 170 | 25.192.550 B | 252.320 B | 0,8763 s | 2.746.774 B |

Before/after tương đương vì không triển khai cache runtime; không phải tốc độ cloud hay bằng chứng tiết kiệm thật. A: giữ implementation đơn giản, tốn reads lặp. B: cache COMPLETED có thể giảm reads nhưng chưa chứng minh tính bất biến, version/revocation hay phân quyền tenant; do đó disabled. Nếu phát triển: key gồm dataset/version/permission scope, xác thực trước cache, TTL ngắn, byte limit, LRU, defensive copy, invalidate theo version, không cache partial/failed; test race, eviction, tenant. Multi-worker/Vercel cache có thể mất và không chia sẻ. C: query-friendly schema, database cursor hoặc search index có thể giảm reads nhưng cần migration, index, security, full-text semantics và đánh giá chi phí; không triển khai.

## Shared dataset — đề xuất

Source dataset immutable nhận diện bằng source, country, access, project, date/window, source/cleaning version; analysis dataset thêm taxonomy/classification version, algorithm/version/config. Contract canonical JSON/hash thử nghiệm; thay scope/version/config đổi key, run ID không phải dataset ID. Chỉ chia sẻ nếu source license, quyền, data ownership và policy cho phép. Tenant settings, filter và saved research tách riêng. Run metadata giữ requester, job, provenance; active pointer có thể trỏ immutable dataset sau validation. Chống trùng fetch cần transaction/lease/fencing giai đoạn sau. Retention phải xét license, references, legal deletion và active pointer; chưa xây cleanup. Một dataset dùng N lần có thể tránh N−1 lần ghi/fetch nếu cùng identity, nhưng list reads không tự giảm; không cam kết lợi ích thực tế. Cần thiết kế/test tenant isolation và invalidation.

## Kiểm thử và giới hạn

- File mới: `Backend/app/storage_analysis/{__init__,metrics,planning}.py`, `Backend/scripts/benchmark_storage_offline.py`, `Backend/tests/test_storage_analysis.py`, báo cáo này.
- Test counting thành công/thất bại/rỗng, byte Unicode, concurrency/isolation, HTTP pagination/chi tiết, scenario/budget, dataset identity/license; không gọi dịch vụ thật.
- Toàn bộ test 132 pass, 0 fail/skip; pip check, bash -n, node --check pass; golden 7/7 cho mỗi hash seed 1 và 983. Golden expected, API, schema, dashboard, local/Vercel không thay đổi.
- Chưa đo retry/actual cloud billing, index/storage/egress; chưa test cache vì không triển khai. Mô hình không bảo đảm quota thực; cần đo cloud có phép riêng trước tối ưu hoặc bật guard.
- Bước 12 đạt phạm vi đo và thiết kế offline; Bước 13 chỉ bắt đầu khi chấp nhận giới hạn trên và có yêu cầu lease/fencing/transaction riêng. Không có scheduler, TTL, migration hoặc thay IAM/billing.
