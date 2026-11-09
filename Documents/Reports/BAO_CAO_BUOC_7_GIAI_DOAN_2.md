# Giai đoạn 2 — Bước 7: Layered Cleaning Pipeline

**Ngày:** 09/11/2026. Baseline `fbdf112`. Phạm vi offline: không gọi Wikimedia/Firestore/AI, thay scoring/taxonomy/API/schema, deploy hoặc bật billing.

## 1. Kiến trúc trước/sau

Trước: Wikimedia adapter `_parse` validate payload/count/date và tạo `PageObservation`; `clean_snapshot` trong `processing/wikimedia_cleaning.py` kết hợp validation, percent decode/NFC, technical rules, dedup và report. Sau đó `extract_keywords` tạo ID và phân loại v0.1, scorer xử lý missing/censored; RunService persist snapshot/observation/result. Adapter phân biệt `DAY_UNAVAILABLE`, `FETCH_ERROR`, `NOT_IN_TOP` và observed. Cleaning không phân loại chủ đề.

Sau:

```text
Wikimedia adapter: payload/schema → snapshot + observations
 → clean_snapshot (compatibility entry point)
 → process_snapshot: schema → source validation → normalization
   → injected Wikimedia technical rules → exact dedup → quality report
 → keyword identity + title-rules-v0.1 → trend v0.1 → storage port
```

`ProcessingContext`, `ProcessingStage`, `StageResult`, `RejectionReason`, `QualityReport`/`CleaningReport`, `PipelineResult`/`CleaningResult` tại `processing/contracts.py`. Stages là function nhỏ; lỗi stage bất ngờ được truyền ra, không âm thầm bỏ record. `source_rule` và `validate_source` là tham số của pipeline để các nguồn sau thay bằng semantics riêng; model observation vẫn theo contract hiện tại; nguồn mới phải cung cấp validator riêng, chưa có schema phổ quát cho mọi API. Không có dependency HTTP/Firebase trong processing.

`app/sources/wikimedia/cleaning_rules.py` chứa namespace/localized rules `wikimedia-technical-pages-v0.1`. Wrapper giữ import cũ và chỉ áp rule này cho Wikimedia source; nguồn khác không bị technical-page rules của Wikimedia. Không thêm rule mới, không thay mặc định demo. `composition.py` tiếp tục inject `clean_snapshot` cũ vào RunService; không thay dashboard, API, status hoặc Firestore schema.

## 2. Hành vi và provenance

- Schema/source validation giữ thứ tự lý do v0.1, phân biệt missing views, invalid rank/views, timestamp và scope; 0 hợp lệ. Adapter vẫn chịu trách nhiệm validate raw response và báo record bị từ chối.
- Normalization giải mã percent-encoded một lần, NFC và thay underscore/whitespace; không dịch. `article_original`, raw record, source, scope, project, observation date, rank, views và evidence URL được giữ; canonical/display là bản đã normalize. Keyword/observation IDs tiếp tục từ original identity, không phải title đã dịch.
- Exact dedup dựa trên `(source, country, access, project, article_original, observation_date)`, chỉ trong từng snapshot, first wins; khác source/project/scope/ngày không gộp. Dữ liệu trùng vẫn được giữ trong `duplicates`; bản bị loại giữ original và reason. Không fuzzy matching.
- `CleaningReport.pipeline_version` là property `cleaning-pipeline-v0.1`; rule version `wikimedia-technical-pages-v0.1` tại module rule. Không thêm field vào JSON/Firestore report v0.1. Report có raw/accepted/rejected/duplicate, reasons và flags; số lượng rejected gồm adapter rejected, có thể thiếu raw detail và được gắn flag. Một flag có thể áp cho nhiều điều kiện; không cộng flags thành số record.

Golden fixture **synthetic** Bước 2:

| Tổng raw | Accepted | Rejected | Duplicate |
|---:|---:|---:|---:|
| 13 | 6 | 6 | 1 |

Rejected: 2 `ADAPTER_INVALID`, 1 `TECHNICAL_MAIN_PAGE`, 1 `TECHNICAL_NAMESPACE`, 2 `TECHNICAL_LOCALIZED_MAIN_PAGE`. Flags bao gồm `SOURCE_PARTIAL_OR_ERROR`, `DUPLICATES_REMOVED_FIRST_WINS`; tổng = accepted + rejected + duplicate. Với ngày 404/429, counts không xác định là `None`, không chuyển thành 0; test 200 rỗng cho count 0.

## 3. Known issue và phương án sau

Tests mới ghi nhận `Wikifunctions:Main_Page`, `Wiktionary:Main_Page`, `Wikisource:Main_Page` vẫn được giữ theo rule v0.1; báo cáo demo đã nêu đây là các trang **có vẻ** kỹ thuật. Đề xuất nghiên cứu rule v0.2 theo cặp project/namespace/title được xác nhận bằng metadata nguồn và xem xét từng project; không match substring chung `Main` hoặc `Special`, vì `Main_Pageant`/`Special_Relativity` là title nội dung hợp lệ. Chưa xác thực metadata live hay bật v0.2, do đó false positive/false negative cần đánh giá trước thay đổi.

## 4. File và kiểm thử

- Mới: `Backend/app/processing/contracts.py`, `pipeline.py`, `Backend/app/sources/wikimedia/cleaning_rules.py`, `Backend/tests/test_cleaning_pipeline.py`, báo cáo này.
- Sửa: `Backend/app/processing/wikimedia_cleaning.py` thành wrapper tương thích và re-export các class/rule cũ.
- **91 pass, 0 fail, 0 skip** (85 cũ + 6 test mới); golden 7 tests pass ở `PYTHONHASHSEED=1` và `983`. Output hai process giống nhau, SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, khớp Bước 2–6; expected không thay.
- `pip check`, `bash -n setup.sh start.sh`, `node --check Frontend/public/app.js` pass. Tests mới kiểm tra missing/zero, Unicode/percent, provenance, source rule isolation, identity nhiều scope, reconciliation, missing/404/429, empty batch, stage failure và known issue; tests cũ bảo vệ API/Firestore giả lập/score/classification.
- Local/test/Vercel read-only chỉ kiểm qua suite offline/mock. Không chứng minh WIF/IAM/Firestore live, browser hay fetch live mới. Không tạo các stage language detection, near-duplicate, translation hoặc AI.

## 5. Rủi ro và Bước 8

`views_ceil` xấp xỉ, mẫu top bị censored, bản không trong top không là 0; report quality không đo độ chính xác toàn thị trường. Source khác cần cung cấp validator/semantics riêng, không tái sử dụng validator Wikimedia theo kiểu máy móc. Technical-page false negative và false positive chỉ có thể được đánh giá bằng mẫu có provenance/review. Trước Bước 8, duyệt ngôn ngữ, corpus và ngưỡng, tài nguyên và quyền dữ liệu; giữ golden v0.1 và không tự động gộp entity theo similarity.

**Đánh giá:** PASS phạm vi offline; không khẳng định cloud live hoặc đã giải quyết technical-page known issue.
