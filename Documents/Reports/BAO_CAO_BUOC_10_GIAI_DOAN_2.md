# Giai đoạn 2 — Bước 10: Robust Z/MAD và EWMA

**Ngày:** 10/11/2026. **Baseline:** `c29d7aa`. Kết quả dưới đây là synthetic/offline, không phải đánh giá trên dữ liệu thực có nhãn. Không gọi Wikimedia/AI, ghi Firestore, thay scoring/API/schema, bật billing hay deploy.

## 1. Kiến trúc và điều kiện an toàn

Hai strategy được đăng ký trong registry hiện hữu qua `composition.algorithm_engine()` nhưng **không được chọn trong đường chạy demo**: RunService tiếp tục gọi duy nhất `legacy_results` và không lưu kết quả thống kê mới. Runner chọn keys tường minh; một strategy skip/fail không làm mất strategy khác. Không thêm algorithms CUSUM/Kleinberg/change point/Bayesian vào registry. Contracts, runner và chiến lược không dùng Firebase/FastAPI/network.

`AlgorithmInput.current_snapshot_status` mới có default `UNKNOWN`; hai strategy yêu cầu caller xác nhận `SUCCESS`. Existing algorithms không dùng field này, API/Firestore schema không đổi. Mỗi history point phải cùng source/country/access/project/article/metric/unit, là `OBSERVED` hợp lệ, snapshot `SUCCESS`, ngày duy nhất trong window **liên tục mỗi ngày**, không future/current; thiếu ngày, not-in-top, 404, lỗi hay partial → SKIPPED, không nội suy hoặc gán 0. Giá trị 0 quan sát thật được chấp nhận.

Vì source hiện tại là top-list censored, hai config mặc định `allow_top_censored=False`: ngay cả chuỗi đầy đủ chỉ được tính khi caller chọn strategy **và** bật `allow_top_censored=True` một cách tường minh trong context thử nghiệm. Kết quả khi opt-in luôn kèm `CENSORED_TOP_LIST`, `VIEWS_APPROXIMATE`, `CONDITIONAL_ON_TOP`, `NOT_SIGNIFICANCE_TEST`; không tuyên bố xu hướng có ý nghĩa thống kê. Nhiều lần vào top liên tục vẫn có selection bias và `views_ceil` là cận trên xấp xỉ. Lựa chọn tạm này không chứng minh mô hình hợp lệ cho inference.

## 2. Robust Z/MAD

- ID `robust-z-mad@v0.1`; config `robust-mad-config-v0.1`, minimum **14 ngày lịch sử liên tục**, window ≤90. Đây là ngưỡng kỹ thuật thận trọng ban đầu để tránh ép chạy trên window 7 ngày, **không phải cỡ mẫu được NIST chứng nhận hay bảo đảm độ chính xác**; cần evaluation ở Bước 11.
- Với lịch sử trước current: `median = median(history)`, `MAD = median(abs(x - median))`, `z = 0.67448975 * (current - median) / MAD`. Không đưa current vào baseline. Nếu MAD = 0 trả `SKIPPED/ZERO_MAD`, không dùng epsilon. Z có thể âm hoặc >100, unit dimensionless, không là trend score.
- Ví dụ synthetic: lịch sử 1…15 → median 8, MAD 4; current 15 → z = `0.67448975 × 7/4 = 1.1803570625`; current 1 → z âm đối xứng; current 8 → 0. Test tính công thức độc lập; current 100000 không bị clip.

## 3. EWMA

- ID `ewma@v0.1`; config `ewma-config-v0.1`, `alpha=0.3` mặc định, `0 < alpha ≤ 1`, warmup tối thiểu **7 ngày lịch sử liên tục**, window ≤90, cadence một ngày. Ngưỡng warmup là policy thử nghiệm chứ không bảo đảm ý nghĩa thống kê; cần chọn/đánh giá trên dữ liệu phù hợp.
- Khởi tạo bằng observation lịch sử sớm nhất; chạy theo ngày tăng: `E_t = alpha*x_t + (1-alpha)*E_(t-1)`. `baseline_value` là historical EWMA chỉ từ lịch sử, `value` là EWMA sau khi cập nhật current; cùng unit `approx_pageviews_upper_bound` với đầu vào, không là score 0–100. Evidence ghi alpha, warmup, cadence, init, số mẫu lịch sử/tổng, scope, history và URL.
- Ví dụ synthetic với lịch sử `[1,2,3,4,5,6,7]`, current 8, alpha 0,5: historical EWMA `6.015625`, current EWMA `7.0078125`; alpha 1 trả 8. Tests có chuỗi tăng/giảm/hằng và đảo thứ tự đầu vào.

Theo [NIST về modified Z/MAD](https://itl.nist.gov/div898/handbook/eda/section3/eda35h.htm) và [EWMA/exponential smoothing](https://itl.nist.gov/div898/handbook/pmc/section4/pmc431.htm), công thức và khởi tạo cần được giải thích; các ngưỡng 14/7 và policy censoring ở đây là **quyết định thử nghiệm của dự án**, chưa được hiệu chuẩn trên Wikimedia. Tài liệu tham khảo đối chiếu 10/11/2026.

## 4. Status và evidence

| Điều kiện | Kết quả |
|---|---|
| Đủ lịch sử liên tục, config hợp lệ, current xác nhận, opt-in censoring | `COMPLETED`, value/statistic + history/config/provenance, quality flags. |
| Config/alpha/warmup sai | `SKIPPED/INVALID_CONFIG`, value null. |
| Dưới warmup/minimum | `SKIPPED/INSUFFICIENT_SAMPLES`. |
| Thiếu ngày, NOT_IN_TOP/404/error hoặc partial | `SKIPPED/MISSING_DAYS`, `CENSORED_OR_MISSING` hoặc `PARTIAL_COVERAGE`. |
| Chưa xác nhận snapshot current hoặc chưa opt-in top-censored | `SKIPPED/CURRENT_COVERAGE_UNCONFIRMED` hoặc `CENSORED_SOURCE_NOT_ACCEPTED`. |
| MAD 0 | `SKIPPED/ZERO_MAD`; evidence có median và MAD 0. |
| Trùng ngày, khác scope/unit, invalid hoặc future | `SKIPPED` với `DUPLICATE_DATE`, `INCOMPATIBLE_OBSERVATION`, `INVALID_OBSERVATION` hoặc `INVALID_WINDOW`. |
| Lỗi code strategy | `FAILED/STRATEGY_ERROR`, value null; runner không lộ thông tin exception. |

Skip details gồm config/version, scope và lịch sử để đối chiếu; `sample_count` đếm observation hiện có. Không biến SKIPPED/FAILED thành 0. Window demo hiện chỉ 7 ngày: Robust Z/MAD mặc định không đủ 14; EWMA chỉ đủ nếu cả 7 ngày liên tục, current snapshot xác nhận và có opt-in censoring; mặc định vẫn skip.

## 5. Specification tương lai — chưa triển khai hoặc đăng ký

| Thuật toán | Mục tiêu, input/cadence/unit và điều kiện skip | Rủi ro censoring, output và chi phí |
|---|---|---|
| CUSUM | Phát hiện lệch tích lũy so với baseline xác định; cần chuỗi đủ dài, baseline ổn định, cadence đều, metric/unit cùng scope và ngưỡng hiệu chỉnh; thiếu baseline/chuỗi skip. Cỡ mẫu/ngưỡng phải được đánh giá, chưa chốt. | Chỉ thấy trang lọt top có thể tạo điểm gãy giả; output statistic/tín hiệu + threshold, không chứng minh demand; CPU O(n), memory nhỏ. Cần baseline, label và kiểm định false alarm. |
| Kleinberg | Phát hiện burst từ chuỗi sự kiện hoặc counts theo thời gian; cần khoảng thời gian/exposure, event ordering và đủ events; thiếu timestamps/cadence hoặc đơn vị không tương thích skip. | Cận trên pageviews và top censoring không tự tương đương event stream; output khoảng burst/level, không doanh số; CPU/memory phụ thuộc trạng thái và số điểm. Cần xác nhận mô hình dữ liệu và tham số. |
| Change Point | Tìm điểm đổi phân phối trong chuỗi đủ dài/cadence ổn định; min sample và penalty tùy phương pháp, cần xác định qua thử nghiệm; thiếu dữ liệu/cadence skip. | Cắt ngưỡng top có thể tạo change point giả; output vị trí/độ mạnh/phương pháp, không xác suất mặc định; CPU/memory phụ thuộc thuật toán, có thể lớn. Cần dataset, test ngoài mẫu và guard. |
| Bayesian Count | Mô hình count có exposure, prior, dispersion và đơn vị phù hợp; thiếu count thực/exposure hoặc prior chưa duyệt skip; số mẫu tối thiểu tùy model. | `views_ceil` xấp xỉ/top-censored không phải exact count, cần likelihood censored được chứng minh; output posterior/uncertainty, không điểm bán hàng; CPU/memory từ thấp đến cao theo inference. Cần model review, calibration và cost budget. |

## 6. Files, kiểm thử và Bước 11

- Mới: `Backend/app/algorithms/statistical.py`, `Backend/tests/test_statistical.py`, báo cáo này.
- Sửa: `Backend/app/algorithms/contracts.py` (current snapshot status), `runner.py` (skip evidence/config có thể cung cấp bởi strategy), `Backend/app/composition.py` (register hai strategy). Không sửa scorer v0.1 hay golden expected.
- **117 pass, 0 fail, 0 skip**: 109 cũ + 8 test mới, gồm tính tay MAD/EWMA, zero, censoring, missing, scope, date, alpha, warmup, registry/runner và reproducibility. Suite dùng synthetic/fake; không chứng minh Firestore/WIF/cloud live.
- Golden 7 tests pass với `PYTHONHASHSEED=1` và `983`; output bằng nhau, SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, giữ Bước 2–9. `pip check`, shell syntax và Node syntax pass.
- Known issues: thiếu dữ liệu chuỗi liên tục, censoring/rounding/seasonality có thể làm số thống kê gây hiểu nhầm; chưa có labeled evaluation, false-positive rate hay so sánh v0.1; technical-page issues vẫn tồn tại. Để sang Bước 11 cần thống nhất ground truth, nguồn/license, phân chia thời gian, metric đánh giá, tiêu chí precision/coverage và review, không tự bật strategy production.

**Đánh giá:** PASS offline cho contract/toán học theo policy thử nghiệm; chưa chứng minh hiệu quả dự báo hoặc ứng dụng thương mại.
