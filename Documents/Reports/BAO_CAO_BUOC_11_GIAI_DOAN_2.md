# Giai đoạn 2 — Bước 11: Đánh giá thuật toán offline

Trạng thái: kiểm thử cơ chế offline bằng synthetic; chất lượng trên dữ liệu thật **NOT_EVALUATED**. Không gọi Wikimedia, Firestore hay AI; không đổi API, runtime, scoring hoặc golden expected. Chưa có cơ sở kết luận thuật toán nào tốt hơn.

## Audit rò rỉ Bước 10

Fixture `test_statistical.series` dùng ngày phân tích 31/01/2026. Với history 1–15: ngày 16–30/01, current=15 ngày 31/01 là quan sát riêng dù trùng giá trị; median=8, MAD=4, z=1,1803570625. Với history 1–14: ngày 17–30/01, median=7,5, MAD=3,5, z≈1,4453351786. Test xác nhận cả hai; current hoặc future trong history bị `INVALID_WINDOW`, ngày trùng bị `DUPLICATE_DATE`. Thay current không đổi historical EWMA. Không phát hiện leakage trong các trường hợp kiểm tra; không chứng minh mọi pipeline tương lai không rò rỉ.

## Dataset, review và lấy mẫu

Fixture `Backend/tests/fixtures/evaluation_v1/synthetic.json` ghi dataset ID/version, policy version, mode, evaluation date/window, scope (source/country/access/project/metric/unit), entity ID/title/provenance, current và history, label, evidence_as_of, reviewer ID, review timestamp có timezone và disagreement. Validator từ chối scope khác, entity/ngày trùng, current sai ngày, history cùng ngày/tương lai/ngoài window, evidence sau evaluation date, nhãn không hợp lệ hoặc thiếu reviewer. `UNCERTAIN` và `NOT_REVIEWED` không là negative. Bất đồng chưa giải quyết phải là UNCERTAIN. Review được phép sau evaluation date nhưng evidence phải giới hạn tại evaluation date; điều này vẫn cần audit thủ công. Không dùng taxonomy hay output algorithm làm ground truth. Không truyền labels vào algorithm; evaluation nhận kết quả đã tính riêng.

Policy đề xuất: reviewer xem title, source, scope, thời gian và evidence gốc đến cutoff; POSITIVE là sự gia tăng chú ý Wikipedia đáng chú ý, có bằng chứng độc lập đã định nghĩa trước; NEGATIVE là đã review và có bằng chứng không thỏa, UNCERTAIN khi thiếu/ngược bằng chứng, NOT_REVIEWED khi chưa review. Wikipedia attention không phải nhu cầu mua hàng. Hai reviewer độc lập, ghi bất đồng; chỉ giải quyết sau adjudication. Chọn mẫu theo source/ngày/volume và nhiều khoảng score, gồm mẫu ngẫu nhiên không chỉ top, cả negative; ghi seed, tỷ lệ lấy mẫu và nonresponse. Cần chốt ngưỡng nghiệp vụ và quyền dữ liệu trước khi tạo nhãn thật.

Không dùng future observation hoặc nhãn làm feature; không tune threshold trên test. Khi tuning, khóa policy/version và chia train/validation/test theo thời gian, đảm bảo window theo entity không overlap. Module chưa triển khai training hay split validator; phải kiểm tra split độc lập trước tuning. Validator không thể chứng minh reviewer độc lập hay evidence xác thực.

## Metric và ranking

Chỉ xếp COMPLETED có số hữu hạn, sắp giảm và hòa theo entity ID tăng. Report giữ considered, completed, skipped, failed, reason, coverage=completed/considered, skip và failure rate; coverage nhãn đếm riêng. SKIPPED/FAILED có giá trị null và reason, không biến thành 0. Mỗi entity cần kết quả tường minh. So sánh trên **giao** entity COMPLETED, cùng dataset, scope, metric/unit đầu vào, ngày và window. Không cộng hoặc so trực tiếp các metric đầu ra khác đơn vị; chỉ so ranking theo nhãn. Tập giao có selection bias, cần báo cả coverage và exclusion.

Precision@K = positive/K, chỉ trả số nếu đủ K kết quả và cả K có nhãn binary. Thiếu kết quả: `INSUFFICIENT_RESULTS`; thiếu nhãn: `INSUFFICIENT_LABELS`, precision=null. K≤0, duplicate, null COMPLETED, unit/scope sai: lỗi validation. Đủ nhãn nhưng không có positive thì precision=0 hợp lệ. False positive count đếm NEGATIVE trong top K đã review; share chia số NEGATIVE cho số binary-reviewed trong top K. Đây không phải FPR thống kê và chỉ có ý nghĩa có điều kiện khi thiếu nhãn.

Policy cố định: volume-only xếp `views_ceil`, metric xấp xỉ, cùng source/scope; Wikimedia v0.1 xếp `trend_score` (0–100); growth-ratio xếp ratio giảm. Adapter `prediction_from_result` giữ algorithm/version, metric/unit, status và context; caller phải cung cấp context chính xác và feature dates. Median baseline và EWMA là mức nền; robust z chưa chọn positive/negative/absolute anomaly nên cả ba `NOT_COMPARABLE`. Không bật strategy trong demo. Số liệu v0.1 chỉ có ý nghĩa nếu input là kết quả thực của strategy; evaluation không tự chạy hay xác thực lại scorer.

## Kết quả synthetic

Fixture có 6 entity, 4 binary-reviewed, 1 uncertain, 1 chưa review. Ví dụ mỗi ranking có 5 completed và 1 skipped, coverage 5/6, skip 1/6; K=4 top A,B,C,D có 2 positive, 2 negative: P@4=0,5, false positives=2, share reviewed=0,5. K=5 chứa UNCERTAIN nên precision=null, `INSUFFICIENT_LABELS`; K=10 `INSUFFICIENT_RESULTS`. Test giao tập có một thuật toán failed entity A: giao B,C,D,E, P@3=1/3 cho volume. Prediction ví dụ là synthetic để test calculator, không phải benchmark scoring thật và không chứng minh accuracy hoặc thứ hạng thuật toán. Precision@1, @5, @10 và không có positive được test với kỳ vọng tính bằng tay.

## Kiểm thử và giới hạn

- Toàn bộ unittest: 127 pass; `pip check` không lỗi; `bash -n setup.sh start.sh` và `node --check Frontend/public/app.js` pass.
- Golden contract với `PYTHONHASHSEED=1` và `983`, mỗi lượt 7 test pass; expected không thay đổi.
- File mới: `Backend/app/algorithms/evaluation/`, `Backend/tests/fixtures/evaluation_v1/synthetic.json`, `Backend/tests/test_algorithm_evaluation.py` và báo cáo này.
- Chưa có uncertainty interval, sampling weights, xác minh nguồn label, hay đánh giá bằng nhãn thật. Review timestamp/evidence_as_of và feature dates là cam kết của caller, không chứng minh raw evidence không rò rỉ. Chưa xác minh split tự động. Chưa benchmark scoring thật trên dataset nhãn thật; không diễn giải fixture predictions là accuracy thực.
- Đánh giá thực cần dataset độc lập đủ lớn, sampling/version, policy nhãn và adjudication được duyệt, quyền sử dụng evidence, provenance thật và temporal split không overlap; sau đó báo coverage và bất định.
- Bước 12 bắt đầu khi phạm vi offline được chấp nhận và các giới hạn được ghi nhận; không thực hiện tối ưu storage trong bước này.
