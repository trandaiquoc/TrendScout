# Giai đoạn 2 — Bước 8: Language, near-duplicate và data quality

**Ngày:** 09/11/2026. Baseline `cb17e6e`. Mọi ví dụ và benchmark trong báo cáo là **SYNTHETIC**, hoàn toàn offline; không gọi nguồn/Firestore/AI, thay API/schema, scoring, classification hay deploy.

## 1. Kiến trúc và cấu hình

`processing/language_detection.py`, `similarity.py` và `quality_analysis.py` tạo **sidecar** nhận `KeywordResult` đã được cleaning/extraction cùng `CleaningReport`; không được gắn mặc định vào RunService và không persist hoặc thay object đầu vào. `AnalysisConfig` version `quality-sidecar-v0.1` có ba cờ độc lập `language_enabled`, `similarity_enabled`, `noise_enabled`; cả ba mặc định `False`. Bật bằng cách gọi nội bộ `analyze(rows, report, AnalysisConfig(...))`, không có API/config environment mới. Mỗi record sidecar giữ object keyword với source, scope, project, title gốc, ngày, rank, views, raw và URL; report ghi cleaning version. Stage thất bại đánh dấu `FAILED` và giữ dữ liệu gốc, không log text/secret exception.

## 2. Nhận diện ngôn ngữ

Detector `lexical-hints-v0.1` dùng tập nhỏ function words tiếng Anh/Việt, NFC và ngưỡng tối thiểu bốn từ/16 ký tự; cần ít nhất hai dấu hiệu phân biệt và không có dấu hiệu trộn để gợi ý `en`/`vi`. Chỉ là heuristic, **không có confidence xác suất**, trả `None`. Title ngắn, tên riêng, số/ký hiệu, chữ non-Latin, ngôn ngữ khác và trộn Anh–Việt trả `unknown`; không suy ngôn ngữ từ project. Đây là khả năng gợi ý hạn chế, không phải detector đa ngôn ngữ đã hiệu chuẩn; tên riêng hoặc cụm vay mượn có thể tạo false positive/negative. Status, reason, method, version và original text được lưu trong sidecar.

Không tải model hoặc thêm dependency. Heuristic dễ kiểm soát chi phí nhưng recall/precision chưa đo; thư viện/statistical model offline có thể mở rộng ngôn ngữ sau khi kiểm tra license, size và dữ liệu gán nhãn. Ví dụ [CLD3](https://github.com/google/cld3/blob/master/README.md) là neural language identifier, không được tích hợp ở bước này; cần benchmark ngắn/mixed trước khi chọn. Không dùng AI translation.

## 3. Near-duplicate candidate

`token-jaccard-v0.1`, threshold mặc định **0,8**, dùng NFC/casefold/underscore và tập token; similarity là intersection/union, không phải trend score hay bằng chứng đồng nhất. Bỏ title quá ngắn (<8 ký tự hoặc <2 token), quá dài (>256 ký tự hoặc >40 token). Blocking theo source/country/access và token chung; project khác vẫn có thể thành candidate nhưng `cross_project_review_required`. Cùng keyword ID trên nhiều ngày không được đề xuất là hai entity. Kết quả giữ hai record gốc (bao gồm IDs và provenance), normalized titles, score/threshold, method/version, reason và `unreviewed`. Không merge, xóa, cộng views hoặc đổi baseline/classification.

Giới hạn mặc định: tối đa 1.000 records, 10.000 cặp so sánh, 50 records/posting token, 5 candidate/keyword. Trường hợp vượt batch/comparison/posting trả `SKIPPED` và lý do, không trả kết quả một phần như hoàn chỉnh. Sort xác định theo score/IDs; cap có thể bỏ bớt candidate và là giới hạn recall. Title người/tác phẩm, phần tiếp theo hoặc thương hiệu/danh từ chung có thể giống nhau dù là entity khác; cần review. Không gộp giữa project/ngôn ngữ.

So sánh: [RapidFuzz](https://rapidfuzz.github.io/RapidFuzz/index.html) hỗ trợ nhiều string metrics, MIT; [Jaro–Winkler](https://epub.ub.uni-muenchen.de/13337/1/215.pdf) có thể hữu ích cho tên nhưng prefix giống dễ gây nhầm; token Jaccard minh bạch và không thêm package, đổi lại kém với typo/thứ tự và Unicode không dấu. [MinHash/LSH](https://ekzhu.com/datasketch/lsh.html) hữu ích cho tập lớn hơn nhưng xấp xỉ/cần xác minh candidate, chưa phù hợp bộ title ngắn và quy mô nhỏ đang đo. Các nguồn trên được đối chiếu 09/11/2026; không coi thuật toán nào đã được chứng minh tốt nhất cho domain.

## 4. Flags, report và đối soát

Noise là flag `noise-rules-v0.1` có reason/evidence/source/observation ID: `SHORT_TITLE`, `SYMBOLS_OR_NUMBERS_ONLY`, `LANGUAGE_UNKNOWN`, `NEAR_DUPLICATE_CANDIDATE`, `MISSING_SOURCE_METADATA`, `UNUSUAL_UNICODE`, `POSSIBLE_TECHNICAL_PAGE`. Không flag nào loại record. Ba title `Wikifunctions:Main Page`, `Wiktionary:Main Page`, `Wikisource:Main Page` chỉ được gắn cảnh báo nếu bật noise, vẫn giữ rule cleaning v0.1.

`AdvancedReport` phân biệt raw/accepted/rejected/exact duplicates, số detected/unknown và language distribution, số candidate pairs, số record có flags, tổng flags/phân bố, stage status/reason, số comparisons, versions và elapsed. Một record có thể có nhiều flags/candidates, không cộng chúng thành số bản ghi. Ngày nguồn unavailable: raw/accepted và các count mới là `None`, stage `SKIPPED/source_unavailable`, không giả thành 0. Golden synthetic được kiểm tra lại: **13 raw = 6 accepted + 6 rejected + 1 exact duplicate** sau sidecar.

## 5. Benchmark synthetic và kiểm thử

Trên môi trường Ubuntu hiện tại, Python 3.12, ba lượt/mẫu; thời gian chỉ cho candidate function, input synthetic theo nhóm 20, không có candidate vượt 0,8:

| Records | Comparisons | Candidates | Min–max |
|---:|---:|---:|---:|
| 100 | 950 | 0 | 0,533–0,856 ms |
| 500 | 4.750 | 0 | 3,214–3,476 ms |

Đây không phải benchmark worst-case, RAM, độ chính xác hay khả năng xử lý hàng triệu keyword. Phân phối token phổ biến có thể chạm `posting_limit` và bị skip. Cần bộ dữ liệu có nhãn để đo precision/recall và chọn threshold.

- **100 pass, 0 fail, 0 skip** (91 cũ + 9 tests mới). Bao gồm English/Vietnamese/unknown/mixed, stage failure, multi-flags, cross-project/day/source, similarity threshold, cap, stable ordering, no merge và golden sidecar reconciliation.
- Golden 7 tests pass ở `PYTHONHASHSEED=1` và `983`; output hai process giống nhau, SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, trùng baseline Bước 2–7; không sửa expected.
- `pip check`, `bash -n setup.sh start.sh`, `node --check Frontend/public/app.js` pass. Tests dùng fake/mock; API, Firestore, local và Vercel read-only giữ theo contract cũ, không xác minh cloud live.

## 6. Files và điều kiện Bước 9

Mới: `Backend/app/processing/language_detection.py`, `similarity.py`, `quality_analysis.py`, `Backend/tests/test_advanced_quality.py`, báo cáo này. Legacy pipeline, taxonomy, score, API và Firestore schema không đổi.

Rủi ro còn lại: lexical detector có thể nhầm, threshold/candidate chưa được đánh giá trên dữ liệu nhãn; blocking/cap gây thiếu recall; quality stage fail chỉ báo status chứ không thay thế xử lý lại; 3 trang kỹ thuật còn là known issue. Trước Bước 9 duyệt input contract/metric/version, điều kiện chạy/skip và golden; không chuyển similarity hoặc language detection thành trend score. Thuật toán nâng cao chỉ bật khi có data/budget/evidence.

**Đánh giá:** PASS offline cho sidecar; chưa bật trong demo và chưa nghiệm thu dữ liệu/provider live hoặc độ chính xác ngôn ngữ/similarity.
