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

## Bước 5 — Cleaning và chống trùng

`app/processing/wikimedia_cleaning.py` xử lý từng `DatasetSnapshot` và danh sách `PageObservation` do adapter cung cấp:

1. Đối chiếu `item_count = số observation adapter trả + số adapter từ chối`; không khớp thì báo lỗi, không tự sửa số liệu. Ngày không lấy được có các số `None`.
2. Kiểm tra source, scope, project, article, ngày thuộc snapshot, thời điểm fetch có timezone, rank, lượt xem và raw record. `views_ceil=0` hợp lệ; `None` bị đánh dấu `MISSING_VIEWS`.
3. Chuẩn hóa Unicode NFC, giải mã phần trăm UTF-8 một lần nếu có, thay `_` và chuẩn hóa khoảng trắng. `+` không bị biến thành khoảng trắng; giá trị gốc luôn được giữ. Chuỗi mã hóa sai UTF-8 bị từ chối có lý do. Tài liệu [MediaWiki page title](https://www.mediawiki.org/wiki/Manual:Page_title/en) mô tả title nguồn khác với URL và dấu gạch dưới tương đương khoảng trắng (đối chiếu 09/11/2026).
4. Loại đúng `Main_Page`, dấu `-` và các prefix trong danh sách `TECHNICAL_NAMESPACES`, bao gồm `Special`, `File`, `User`, `Talk`, `Wikipedia`, `MediaWiki`, `Template`, `Category`, `Portal` và một số alias `Special` theo ngôn ngữ. Một title có dấu `:` nhưng prefix không thuộc danh sách vẫn được giữ. Danh sách là rule tĩnh, chưa đầy đủ namespace/alias của mọi project; cần kiểm tra để tránh sót và loại nhầm. [MediaWiki](https://www.mediawiki.org/wiki/Help:Namespaces) cho biết namespace có thể được bản địa hóa.
5. Deduplicate theo `(project, article_original, observation_date)` trong snapshot; giữ bản ghi hợp lệ đầu tiên, trả riêng các bản trùng. Không gộp cùng tên khác project hoặc khác ngày. Đây là so sánh title nguồn nguyên bản; không coi hai title sau normalize giống nhau là cùng trang.

Kết quả gồm các observation hợp lệ và danh sách bản ghi bị loại kèm lý do, bản trùng, bản thô bị adapter từ chối (nếu adapter cung cấp), cùng `CleaningReport`: `raw_count`, `accepted_count`, `rejected_count`, `duplicate_count`, `rejection_reasons` và `quality_flags`. `rejected_count` bao gồm bản bị adapter từ chối; công thức đối chiếu là `raw_count = accepted_count + rejected_count + duplicate_count`. Nếu một snapshot cũ chỉ có số adapter từ chối mà không có bản thô, report gắn cờ thiếu chi tiết. Không áp rule lĩnh vực, không tính trend score và không tự ghi Firestore.

Để chạy kiểm thử và báo cáo live chỉ đọc:

```bash
python -m unittest discover -s tests -v
python -m scripts.wikimedia_cleaning_smoke
```

Lượt smoke gọi lại API thật, do Bước 4 không lưu response; dữ liệu và ngày khả dụng có thể thay đổi. Source `404` được báo riêng, không coi là một snapshot rỗng hay views bằng 0. Lệnh in ví dụ được giữ/bị loại nhưng không lưu dữ liệu; khi cần audit lâu dài phải thiết kế lưu run ở bước sau.

## Bước 6 — Keyword và phân loại chủ đề

`app/analysis/keywords.py` nhận các observation **đã được cleaning chấp nhận** và tạo một `KeywordResult` cho mỗi observation. Không thực hiện scoring, phân loại sản phẩm hay lọc đầu vào. `canonical_keyword` và `display_keyword` giữ title đầy đủ; `search_keyword` chuẩn hóa NFC, casefold và khoảng trắng để hỗ trợ tìm kiếm, không dùng làm khóa gộp.

`keyword_id` là SHA-256 của tuple `(source, project, article_original)` để nhận diện thực thể nguồn qua nhiều ngày. `observation_id` bổ sung country, access và ngày, do đó không gộp mất observation lịch sử. Kết quả giữ project, title gốc, source, ngày, country, access, fetched_at, views_ceil, rank, source_url, raw_record và quality flags. `to_record()` tạo mapping với ngày ISO và evidence rõ ràng, phù hợp với interface lưu document hiện có; Bước 6 chưa ghi vào Firestore.

Phân loại `title-rules-v0.1` là heuristic thử nghiệm, dựa trên các cụm cụ thể được khai báo trong mã:

- `fashion`: ví dụ `fashion week`, `haute couture`, `clothing manufacturer`.
- `home_goods`: ví dụ `washing machine`, `vacuum cleaner`, `kitchen appliance`.
- `other`: chỉ khi có bằng chứng cụ thể về bầu cử, phim/truyền hình hoặc thể thao theo rule; không mặc định mọi mục không khớp là `other`.
- `unclassified`: không đủ bằng chứng từ title.

Một title có thể có cả `fashion` và `home_goods`; `other` và `unclassified` là nhóm fallback. Các title có định danh tác phẩm `(film)`, `(song)`, `(album)` hoặc tương đương không được gán nhãn sản phẩm chỉ do trùng cụm từ. Mỗi evidence ghi `category`, `rule_id` và đoạn khớp; kết quả gắn `TITLE_RULES_UNVERIFIED`. Rule chỉ đọc title, chủ yếu bằng tiếng Anh, có thể sai hoặc bỏ sót và chưa được đánh giá trên tập dữ liệu gán nhãn. Không diễn giải nhãn là khẳng định chắc chắn hay tín hiệu trend.

`filter_keywords(results, category)` hỗ trợ `all`, `fashion`, `home_goods`, `other`, `unclassified`; `all` giữ toàn bộ kết quả. Nhóm rỗng trả `NO_MATCHES_IN_ANALYZED_DATA`, chỉ phản ánh tập đã phân tích; caller phải báo riêng nếu nguồn không có dữ liệu. Không có dashboard hoặc API tìm kiếm ở bước này.

```bash
python -m unittest discover -s tests -v
python -m scripts.wikimedia_keywords_smoke
```

Script smoke gọi lại Wikimedia, làm sạch rồi phân loại; in số observation và số entity riêng cùng ví dụ, không ghi Firestore. Một entity có thể có nhiều observation theo ngày; số nhóm có thể chồng lấn khi có nhiều nhãn. Nếu nguồn trả thiếu ngày hoặc lỗi, số liệu chỉ phản ánh các snapshot lấy được.
