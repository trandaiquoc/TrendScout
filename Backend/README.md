# TrendScout backend — Bước 3

Backend Python 3.12 + FastAPI, với repository Firestore và fake repository phục vụ kiểm thử. Chưa triển khai fetch Wikimedia, cleaning, scoring, API run hay frontend. Khởi động backend không tự kết nối Firestore.

## Thiết lập trên Ubuntu

Cần Python 3.12 và gói venv tương ứng. Nếu `python3.12 -m venv` báo thiếu `ensurepip`, cài `python3.12-venv` từ kho Ubuntu đã cấu hình (ví dụ `sudo apt install python3.12-venv`). Nếu bản Ubuntu không có Python 3.12, cài Python 3.12 và gói venv tương thích từ nguồn phù hợp.

Từ `Backend/`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Terminal khác: `curl -i http://127.0.0.1:8000/api/health`. Kết quả dự kiến HTTP 200 và `{"status":"ok"}`; endpoint chỉ kiểm tra backend phản hồi, không kiểm tra Firestore hoặc Wikimedia. `TREND_SCOUT_APP_NAME` là biến môi trường tùy chọn cho tiêu đề FastAPI.

## Firestore và ADC

Đối với project đã có và database `(default)`:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="trendscout-192e4"
```

`FirestoreStorage.from_environment()` đọc project từ `GOOGLE_CLOUD_PROJECT`, khởi tạo Firebase Admin SDK qua Application Default Credentials (ADC) và dùng database `(default)`. Không cần nhúng credential vào mã. Trên môi trường chạy khác dùng danh tính service account và ADC thích hợp; nếu dùng file credential, lưu ngoài repository. Không commit hoặc gửi nội dung credential. `.gitignore` chặn các tên phổ biến, không thể nhận diện mọi tên file key tùy ý.

Ví dụ gọi repository từ code async (đây là ví dụ giao diện, không tự chạy khi khởi động):

```python
from app.storage.firestore import FirestoreStorage

store = FirestoreStorage.from_environment()
await store.create_run("run-id", {"status": "PENDING", "source": "wikimedia"})
await store.update_run("run-id", {"status": "RUNNING"})
run = await store.get_run("run-id")
```

**Quyền:** Firebase Admin/server SDK dùng ADC và IAM, bỏ qua Firestore Security Rules dành cho mobile/web. Rules `allow read, write: if false` không ngăn backend có quyền IAM. Dùng principal riêng và IAM tối thiểu phù hợp; không dựa vào client rules để bảo vệ backend. SDK chạy trong server, không đưa credential vào frontend. Xem [Firebase Admin SDK](https://firebase.google.com/docs/admin/setup) và [Firestore IAM](https://docs.cloud.google.com/firestore/native/docs/security/iam). Tài liệu được đối chiếu ngày 09/11/2026.

## Cấu trúc lưu trữ

```text
analysis_runs/{run_id}                              trạng thái, source metadata, thời điểm, lỗi
  snapshots/{snapshot_id}                           ngày nguồn, trạng thái fetch, phạm vi, số bản ghi
  observations/{observation_id}                     project, article, ngày, views_ceil, rank, quality flags
  keyword_results/{keyword_id}                      keyword, score nullable, status, evidence, flags
  baseline_buckets/{bucket_id}                      baseline nullable, số ngày quan sát, cửa sổ, flags
```

Các field là thiết kế dự kiến cho bước tiếp theo; repository hiện lưu và trả mapping mà caller cung cấp, **chưa validate schema nghiệp vụ hay tự tạo metadata, baseline hoặc score**. Dùng ngày dạng chuỗi ISO `YYYY-MM-DD`; timestamp dùng `datetime` có timezone UTC. Key của observation cần được caller tạo ổn định từ project, article và ngày; không dùng trực tiếp title có dấu `/` làm document ID.

`create_run` chỉ tạo mới; `update_run` cập nhật các field cấp đầu và báo lỗi nếu run chưa tồn tại. `put_record` ghi đè toàn bộ document cùng ID; `get_*` trả `None` nếu không tồn tại. Trường bị bỏ qua là missing, trường truyền `None` được lưu null, còn `0` là 0; update không tự đổi giá trị bị bỏ qua. Subcollection có thể tồn tại ngay cả khi parent không tồn tại; caller chịu trách nhiệm vòng đời run. Các thao tác là từng document, không tạo transaction toàn pipeline; nếu ghi giữa chừng thất bại, caller phải đánh dấu run chưa hoàn tất và xử lý retry. Xóa parent không tự xóa subcollection; hiện chưa có API xóa run.

## Kiểm thử

```bash
python -m unittest discover -s tests -v
python -m pip check
```

Các test dùng fake và mock, không gọi cloud. Để kiểm tra live thủ công sau khi xác nhận project/ADC, chạy từ `Backend/`:

```bash
export GOOGLE_CLOUD_PROJECT="trendscout-192e4"
python -m scripts.firestore_smoke
```

Script chỉ chấp nhận project này, database `(default)` và không cho phép cấu hình emulator. Script tạo một document tên ngẫu nhiên trong `analysis_runs`, đọc/cập nhật, sau đó xóa chính document đó và kiểm tra lại. Nếu bị gián đoạn hoặc lỗi mạng sau khi gửi create, kiểm tra document `step3-smoke-*` được in trong log hoặc Console trước khi dọn; không xóa dữ liệu khác. Kiểm thử live có sử dụng quota Firestore hiện hành. Không cần bật billing cho thao tác trong free quota; tự xác nhận quota và cấu hình thực tế trước mỗi lần chạy.

## Ranh giới source

- `app/api/`: health route; không có API run.
- `app/adapters/`, `app/models/`: interface nguồn và model dự kiến.
- `app/storage/`: contract, Firestore và fake.
- `app/processing/`, `app/analysis/`, `app/orchestration/`: vị trí dành cho các bước sau.

## Bước 4 — Wikimedia top-per-country

Adapter `app/adapters/wikimedia.py` gọi danh sách `US/all-access` không có query lọc chủ đề/ngôn ngữ. Mặc định bắt đầu từ hôm qua theo UTC, thử lùi tối đa 7 ngày khi gặp 404, rồi lấy 7 ngày lịch trước ngày phân tích. Request gửi tuần tự trong từng adapter; không chạy nhiều adapter song song để vượt chính sách nguồn. Lỗi khác 404 khi tìm ngày mới nhất làm dừng tìm kiếm, không âm thầm dùng ngày cũ. Lịch sử có thể thiếu; tổng số request gồm cả retry. Không tự ghi Firestore.

```bash
python -m scripts.wikimedia_smoke
python -m unittest discover -s tests -v
```

`WIKIMEDIA_USER_AGENT` là biến tùy chọn; mặc định `TrendScout/0.1 (https://github.com/trandaiquoc/TrendScout)` theo project thực tế. Khi vận hành bởi bên khác, đặt User-Agent có tên, version và URL liên hệ thực tế. Theo [Wikimedia access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html), gửi tuần tự và tự nhận diện; [404 có thể do dữ liệu chưa tải hoặc không có dữ liệu](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/troubleshooting.html). Đối chiếu ngày 09/11/2026.

Mặc định retry tối đa 2 lần với timeout 15 giây, tuân thủ `Retry-After` của 429/5xx. Nếu thời gian chờ yêu cầu vượt giới hạn 30 giây, trả lỗi thay vì thử sớm. `collect` trả snapshot cho từng ngày kèm HTTP status, số lần request, số mục nhận/hợp lệ/bị từ chối và lỗi; `None` biểu thị không xác định, không phải 0. Nếu một số mục không hợp lệ, snapshot là `PARTIAL`; nếu tất cả mục đều sai, là `FETCH_ERROR`. Không suy `NOT_IN_TOP` từ snapshot lỗi hoặc một phần.

Mỗi observation có phương thức `to_canonical()` gồm `source`, `country_scope`, `access`, `project`, `article_original`, `canonical_keyword`, `views_ceil`, `rank`, `observation_date`, `fetched_at`, `source_url`, `quality_flags` và `raw_record`. Ngày và thời điểm ở dạng ISO; `fetched_at` UTC. `canonical_keyword` hiện chỉ thay dấu gạch dưới bằng khoảng trắng, chưa cleaning hay trích keyword hoàn chỉnh. Khóa định danh là `(project, article_original)`; chưa deduplicate. Giữ `raw_record` cho mọi bản ghi hợp lệ. URL chỉ được tạo cho domain Wikimedia đã biết, nếu chưa xác minh trả `null` và flag. Các flag `TOP_LIST_ONLY`, `VIEWS_APPROXIMATE` nhắc giới hạn nguồn.

`observation_status` phân biệt `OBSERVED`, `NOT_IN_TOP` (chỉ khi snapshot thành công đầy đủ), `DAY_UNAVAILABLE` và `FETCH_ERROR`; không biến bản ghi vắng mặt hoặc ngày lỗi thành lượt xem 0. Giá trị `views_ceil` xấp xỉ, dữ liệu từ top Wikimedia không chứng minh xu hướng mua sắm hoặc social. Ngày dữ liệu là ngày từ response, không phải timestamp của từng page view. Mỗi adapter duy trì lock riêng; hiện chưa có điều phối giới hạn request giữa nhiều process, API công khai, lưu run, cleaning hay scoring.
