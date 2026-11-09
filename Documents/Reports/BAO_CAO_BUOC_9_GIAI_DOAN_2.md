# Giai đoạn 2 — Bước 9: Algorithm Engine và scoring v0.1

**Ngày:** 09/11/2026. Baseline `879cc34`. Phạm vi offline; không fetch Wikimedia, gọi AI, sửa dữ liệu Firestore, thay IAM, bật billing hay deploy.

## 1. Trước/sau và invariants

Trước: RunService nhận trực tiếp `score_trends` qua composition root. Sau:

```text
Source → cleaning → keyword/classification v0.1
       → composition.algorithm_engine → registry → WikimediaLegacy.score_batch
       → cùng TrendResult v0.1 → RunService → shards/API cũ

Offline opt-in: AlgorithmRunner.run(item, selected) → kết quả từng strategy
```

Scorer `app/analysis/trend.py` không đổi: current `views_ceil`, median các ngày quan sát trong 7 ngày trước, tối thiểu 3; không thay missing/NOT_IN_TOP bằng 0. Với current/baseline/max dương: `G=clip(log2(current/baseline)/3,0,1)`, `V=log1p(current)/log1p(max cùng source/country/access/ngày)`, `round(100*(0.7G+0.3V))`. EMERGING khi ratio ≥1,5 và score ≥50; FALLING nếu ratio <0,75; hợp lệ còn lại STABLE; không đủ trả score null/INSUFFICIENT_DATA. Tie sort theo keyword ID ở API. `views_ceil` xấp xỉ, top bị censored; evidence, quality flags, ID và version không đổi.

Đường demo gọi `AlgorithmRunner.legacy_results`, lấy **một** registered strategy rồi gọi scorer batch **một lần**, trả nguyên `TrendResult` cho RunService; không chuyển đổi public payload, không ghi thêm kết quả hoặc chạy hai strategy mới. Lỗi dữ liệu chung của legacy batch vẫn propagate theo semantics RunService cũ. `INSUFFICIENT_DATA` vẫn có mặt trong `all`, score null.

## 2. Strategy contract, registry và runner

`app/algorithms/contracts.py` định nghĩa `Scope`, `Observation`, `AlgorithmInput`, `AlgorithmMetadata`, `AlgorithmResult`, `Strategy` protocol. Scope gồm source/country/access/project/article/metric/unit; observation có date, status, value và snapshot status; input có window/minimum, evidence và optional legacy result. Metadata có ID/version, source, input metric/unit, output metric/unit, sample/window. Result có keyword ID, ID/version, status, value nullable, output metric/unit, current/baseline, window/sample, evidence, quality flags, reason và config version. Value median/ratio không phải trend score.

`AlgorithmRegistry` chỉ đăng ký tường minh, từ chối trùng `ID@version` và unknown. `AlgorithmRunner.run` chỉ chạy keys được chọn, sắp thứ tự xác định, kiểm applicability từng strategy. Không chọn nghĩa là không chạy và không trả SKIPPED. `COMPLETED` có kết quả độc lập; `SKIPPED` có value null và reason, sample/coverage; `FAILED` có value null và thông báo chung đã khử exception. Một strategy lỗi không làm mất kết quả khác; unknown selection bị từ chối trước khi chạy. Không có calls network/Firestore/AI khi import.

| ID | Đầu vào → kết quả | Điều kiện |
|---|---|---|
| `wikimedia-trend@v0.1` | Legacy batch scorer → TrendResult cũ; sidecar map result → trend score `points_0_100` | Wikimedia `views_ceil`, unit `approx_pageviews_upper_bound`, scope/date khớp, window 7/minimum 3; insufficient map SKIPPED trong sidecar, public vẫn INSUFFICIENT_DATA. |
| `growth-ratio@v0.1` | current / median → ratio `dimensionless` | Cùng source/scope/project/article/metric/unit, window 1–7, min ≥3, current số nguyên không âm, median dương. Current 0 hợp lệ và ratio 0; legacy score vẫn có quy tắc current >0 riêng. |
| `median-baseline@v0.1` | median views đã quan sát → `approx_pageviews_upper_bound` | Cùng scope/metric/unit, window/min hợp lệ, ≥3 observations hợp lệ; median 0 là thống kê hợp lệ. |

Ratio/median chỉ được chọn tường minh bằng runner trong test/offline; chưa chạy hoặc persist trên đường demo. Không tổng hợp metric khác đơn vị. `OBSERVED` chỉ chấp nhận số nguyên không âm và snapshot SUCCESS/PARTIAL; ngày NOT_IN_TOP, DAY_UNAVAILABLE, FETCH_ERROR, SNAPSHOT_MISSING, PARTIAL_SNAPSHOT và REJECTED_BY_CLEANING không phải 0. Evidence ghi ngày/value đã quan sát, missing và URL; flags báo censored, approximate, partial/incomplete. Ngày trùng, ngoài window hoặc scope/metric/unit không khớp không được âm thầm gộp.

Các reason gồm `UNSUPPORTED_SOURCE`, `UNSUPPORTED_METRIC`, `UNSUPPORTED_UNIT`, `INVALID_SCOPE`, `INVALID_WINDOW`, `INVALID_CONFIG`, `INVALID_CURRENT`, `INVALID_COVERAGE`, `INVALID_OBSERVATION`, `INCOMPATIBLE_OBSERVATION`, `DUPLICATE_DATE`, `INSUFFICIENT_SAMPLES`, `ZERO_BASELINE`, `LEGACY_RESULT_REQUIRED`, `INSUFFICIENT_DATA`; lỗi bất ngờ `STRATEGY_ERROR`. Metadata và config version xác định ý nghĩa từng kết quả.

Ví dụ synthetic: history 100/200/300, current 800 → median 200 và growth ratio 4,0; history 100/200/300/400 → median 250 và ratio 3,2. Thiếu 3 mẫu → SKIPPED; median 0 → ratio SKIPPED; legacy golden `800/100` vẫn score 100. Đây không phải số liệu live hoặc bằng chứng nhu cầu mua.

## 3. Files và kiểm thử

- Mới: `Backend/app/algorithms/{__init__,contracts,registry,runner,strategies}.py`, `Backend/tests/test_algorithm_engine.py`, báo cáo này.
- Sửa: `Backend/app/composition.py` chọn legacy batch qua engine; scorer, RunService, storage, API, cleaning, classification, sidecar Bước 8 và golden expected không sửa.
- **109 pass, 0 fail, 0 skip** (100 cũ + 9 test engine). Tests synthetic kiểm registry, selection, độc lập/failure sanitization, applicability, mixed scope/source/metric/unit, zero/missing/censored, median chẵn/lẻ, ratio, deterministic order và legacy batch một lần. Test cũ bảo vệ API/schema fake, trạng thái, auth, read-only, persistence giả và classification/scoring.
- Golden 7 tests pass với `PYTHONHASHSEED=1` và `983`; output JSON hai process giống nhau và SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, trùng Bước 2–8. `pip check`, shell/Node syntax pass. Kiểm tra AST import tĩnh không thấy cycle hoặc Firebase/FastAPI/HTTP SDK trong algorithms.
- Local/test/Vercel read-only chỉ được kiểm qua fake/ASGI/mock; chưa có kiểm tra WIF/IAM/Firestore hoặc Vercel live trong bước này.

## 4. Giới hạn và Bước 10

Strategy mới hiện chỉ hỗ trợ semantics Wikimedia và input được caller cung cấp, không tự dựng entity/window từ các source khác. Censoring tạo selection bias; median quan sát không đại diện traffic không lọt top. Lỗi một chiến lược được ghi generic để tránh lộ dữ liệu; observability vận hành chi tiết chưa triển khai. Data quality/language sidecar và classifier v0.2 vẫn tắt trong run demo. Các trang kỹ thuật có thể lọt cleaning theo báo cáo trước.

Chưa đăng ký hoặc triển khai Robust Z/MAD, EWMA, CUSUM, Kleinberg, change point, Bayesian, AI hay composite. Bước 10 cần quyết định dữ liệu tối thiểu/cadence/censoring và kiểm định độc lập trước khi bật từng strategy; giữ golden v0.1 và không trộn metric.

**Đánh giá:** PASS trong phạm vi offline; không phải nghiệm thu cloud hay bằng chứng hiệu quả dự báo/commercial.
