# Giai đoạn 2 — Bước 2: Golden Regression & API Contract

**Ngày:** 09/11/2026. **Baseline:** code `f543399`, audit `b7ce2be`; `origin/main` đã có hai commit Bước 1 trước khi bắt đầu. Phạm vi chỉ gồm fixture tổng hợp, tests và báo cáo; không sửa backend/frontend, scoring, schema, gọi nguồn live, Firestore thật, AI hoặc deploy.

## 1. Fixture và expected

Bộ `Backend/tests/fixtures/golden_v1/` gồm **hai file JSON**:

- `source.json`: `synthetic-wikimedia-v1`, `SYNTHETIC`, thời điểm cố định 09/01/2026 UTC, ngày phân tích 08/01/2026; response US/all-access do `httpx.MockTransport` cung cấp. Có bốn ngày 200 có bản ghi, một ngày 200 rỗng, 404, 429, ngày snapshot vắng; không có request thật. Ngày phân tích gồm 13 rows: `views_ceil=0`, null, sai kiểu, hai project cùng title, Unicode NFC từ percent-encoding, underscore, hai localized main page, `Main_Page`, `Special:Search`, duplicate.
- `expected.json`: cùng version; các trường nguồn/scope/ngày/version và sáu entity gồm ID, title, category, current, baseline, ratio, score, status và evidence URL; report cleaning 13 = 6 accepted + 6 rejected (bao gồm 2 rejected từ adapter) + 1 duplicate. Giá trị là dữ liệu tổng hợp, không đại diện kết quả LIVE hoặc làm đầy nhóm chủ đề của run thật.

Golden hiện có 6 entities, hai project, bảy ngày có response và một ngày snapshot missing. Phạm vi được bổ sung bởi test cũ: multi-label, false positives, timeout, sai timestamp, missing, các ngày và trường hợp nhiều scope. Không đổi expected của tests đã nghiệm thu. `fetched_at` cố định tại mock clock; keyword ID trong expected được tạo theo contract SHA-256 của source/project/article và được kiểm tra cùng observation ID theo source/scope/project/article/date. Tên/hình thức evidence là synthetic; không có key hoặc credential.

## 2. Contract và invariant được bảo vệ

- Adapter validate source, scope/date và các trường; 404 là `DAY_UNAVAILABLE`, 429 là `FETCH_ERROR`; snapshot không có và `NOT_IN_TOP` khác nhau. Timeout/retry đã có trong `test_wikimedia.py`. Kiểm tra raw, rank, fetched_at, project và title gốc.
- Cleaning 6 accepted, 6 rejected, 1 duplicate; nguyên nhân có cấu trúc, identity project/date, percent-decoding và NFC. Cùng keyword ID khác ngày observation ID; cùng title khác project có ID khác.
- Keyword classification version `title-rules-v0.1`, 4 nhóm, evidence và bảo toàn unclassified. Multi-label đã có test riêng trong `test_keywords.py`; không suy luận từ live fixture.
- `wikimedia-trend-v0.1`: median 3 ngày quan sát 100; `800/100 → 100 EMERGING`, `100/100 → 21 STABLE`, `50/100 → 18 FALLING`; current 0/thiếu baseline → score null. Kiểm tra `max_views_ceil=800`, growth/volume, flags censored/approximate và ngày thiếu (404, 429, không top, snapshot thiếu). Test riêng baseline 0/current 0, tỷ lệ 0,74/0,75 và ngưỡng điểm 50; tại ratio 1,5 và max=current, điểm 44 nên status STABLE theo implementation. Test cũ kiểm tra max theo scope, 2 ngày, project, timestamp và các missing khác.
- Độc lập với output dự án: score so sánh bằng phép `log2`, `log1p`, `round` ngay trong test và các giá trị được ghi rõ trong expected; observation ID kiểm SHA-256 từ chuỗi contract. Có negative test thay score và xóa field: cả hai phải khiến comparator báo sai, kể cả khi field lẽ ra là null.
- API qua ASGI transport/fake storage: health, POST thiếu auth và tạo run thành công ở test khác, run ID, 404, validation `422`, list response fields, q, category, empty, limit/offset, detail, sort score-null/zero và tie theo keyword ID. Mock `collect` báo lỗi nếu tìm kiếm gây fetch. Null so với 0 trong test API được mô phỏng bằng dữ liệu shard tổng hợp cố ý, không coi đó là output scoring của observation 0.
- Fake `AnalysisStorage` lưu run/snapshot/observation bucket/16 keyword shards; ghi `COMPLETED` được test chỉ sau khi fake có đủ 8 snapshots, bucket và 16 shards. Các test cũ kiểm tra `FAILED`, `PARTIAL`, `ABORTED`, quota, restart/recovery; test mới dựng app khác với cùng fake để đọc lại. Mô phỏng xóa parent trong fake cho thấy child vẫn tồn tại; fake không có API xóa, test này không chứng minh hành vi Firestore thật. Không có emulator hoặc live test.
- Các test cũ `test_vercel_readonly.py` bảo vệ health công khai, Basic, POST 405, không tạo `HostRunLock`/`RunService`, request OIDC/STS mocked và không ADC fallback. Đây không chứng minh Vercel deployment, IAM hoặc WIF cloud. `test_frontend.py` kiểm tra mount asset qua ASGI; không phải kiểm thử browser mới.

## 3. Chạy lại và kết quả

```bash
cd Backend
.venv/bin/python -m unittest discover -s tests -p 'test_golden_contract.py' -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
cd ..
bash -n setup.sh start.sh
node --check Frontend/public/app.js
```

- Full suite: **56 pass, 0 fail, 0 skip**; 49 cũ + 7 test mới. `pip check`, shell syntax và JS syntax pass.
- Golden 7 test chạy riêng thành công với `PYTHONHASHSEED=1` và `PYTHONHASHSEED=983`. Output `TrendResult.to_record()` đầy đủ ở hai process, JSON sort keys UTF-8, byte giống nhau; SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`. Test lặp lại trong cùng process cũng so sánh đầy đủ output. Kết quả độc lập thời gian hiện tại nhờ mock clock và ngày fixture.
- Network upstream là `MockTransport`; API local dùng `ASGITransport`, repository dùng fake. Không cần ADC, AI key hoặc network, không tạo service cloud và lock file chỉ trong thư mục tạm của test.

## 4. Known issues và giới hạn

- Báo cáo live demo ghi `Wikifunctions:Main Page`, `Wiktionary:Main Page`, `Wikisource:Main Page` có thể còn được giữ; chưa thay cleaning rule hoặc khẳng định đã sửa.
- Wikimedia top là mẫu giới hạn/censored; `views_ceil` xấp xỉ, không thể diễn giải là toàn bộ social trends, doanh số hay lượt xem chính xác. Kết quả lịch sử 7.737 observations, 968 entities là live trước đây, không phải kết quả fixture.
- Basic read-only chỉ là cơ chế demo; WIF/Vercel chưa có cloud live acceptance. Multi-host lock, cleanup đệ quy, IAM, quota và failover thực tế chưa được kiểm chứng trong test này.
- Fake repository tái sử dụng cùng instance khi tái tạo app, không đại diện dữ liệu được lưu qua process hay dịch vụ Firestore; persistence thực là bằng chứng cũ tại báo cáo demo, không phải kết quả Bước 2.
- Test hiện tại là hàng rào được chọn, không chứng minh mọi tiêu chí UI/accessibility, toàn bộ JSON fields hay mọi nhánh upstream. `ABORTED` được kiểm qua test recovery hiện hữu; `PARTIAL`/`FAILED` qua fake lỗi ghi.
- Backend README có phần mô tả Bước 3 cũ nói chưa có run API; source và tài liệu hiện hành có API đó. Không thay tài liệu cũ trong phạm vi test này.

## 5. Trước Bước 3 và nghiệm thu

Giữ golden/version, schema cũ, formula/rounding, ID, source, quality/evidence, missing/censored, API, `./start.sh`, Vercel fail-closed. Khi refactor, chạy full suite và golden hai seeds, so sánh output và báo thay đổi được chủ dự án duyệt; không cập nhật expected chỉ để làm test pass. Trường ngoài golden phải tiếp tục được bảo vệ bằng các test contract cũ hoặc thêm assertion trước khi thay. Không suy app-restart fake chứng minh Firestore persistence.

**Đánh giá:** PASS cho Golden Regression & Contract API trong phạm vi offline được kiểm thử; các giới hạn trên vẫn mở. Có thể chuẩn bị Bước 3 sau khi kiểm tra lại Git/working tree và duyệt phạm vi DI/ports; không cần cấp credential hay live fetch để chạy suite.
