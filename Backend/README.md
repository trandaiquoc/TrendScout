# TrendScout backend — Bước 3

## D4 — Google/Bing SERP ngoại tuyến

`app/sources/serpapi/` là hợp đồng dùng chung cho Google và Bing qua SerpApi candidate; hai adapter chỉ nhận `httpx.MockTransport`, fake resolver và quota pool được inject. Request dùng query thuộc `demo-us-en` v1, endpoint cố định, Google `start` và Bing `first`; provider next URL chỉ được phân tích offset, không được gọi. Mỗi attempt reserve budget, retry/deadline/page cap có giới hạn; record deduplicate URL nhưng giữ query evidence, trạng thái cache và thời điểm provider nếu hợp lệ. Position là vị trí trong mẫu SERP, không phải search volume hay sales. Không có filter bảy ngày khi ngữ nghĩa chưa được xác minh.

Quota pool D4 chỉ process-local, không bảo vệ nhiều host/restart; không được dùng để mở public live Fetch. Cả hai source vẫn disabled, chưa cấu hình, chưa phê duyệt hoặc xác minh live; chúng không được thêm vào registry/Public Fetch. [Capability matrix](../Documents/Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md), [inventory](../Documents/Design/US_PLATFORM_SOURCE_INVENTORY.md), [D5 blueprint](../Documents/Design/D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md).

Để cài đặt và khởi động toàn bộ hệ thống từ root, ưu tiên `./setup.sh` (một lần) và `./start.sh`; xem [README chính](../README.md). Các lệnh dưới đây là cách thiết lập/chạy thủ công.

Backend Python 3.12 + FastAPI có pipeline Wikimedia, Firestore repository, API run và dashboard cùng origin. Khởi động server không tự fetch Wikimedia hoặc tạo run.

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

## Bước 7 — Scoring Wikimedia v0.1

`app/analysis/trend.py` tính heuristic cho các keyword đã qua cleaning trong ngày phân tích. Input là `DayData` cho từng snapshot: snapshot gốc, keyword sau cleaning và các row adapter trước cleaning. Scorer nhóm theo `(source, country_scope, access, project, article_original)` và chỉ dùng profile Wikimedia; không trộn nguồn, project hay phạm vi.

- Baseline là median của tối đa 7 ngày lịch trước đó mà trang có mặt và có `views_ceil` hợp lệ; cần ít nhất 3 ngày quan sát. Giá trị 0 hợp lệ là một quan sát, nhưng current hoặc median bằng 0 không được chia và trả `INSUFFICIENT_DATA`.
- Các ngày thiếu được giữ cùng lý do: `DAY_UNAVAILABLE`, `FETCH_ERROR`, `SNAPSHOT_MISSING`, `NOT_IN_TOP`, `PARTIAL_SNAPSHOT`, `REJECTED_BY_CLEANING`, `INVALID_VALUE`, `DUPLICATE_OBSERVATION`. Chúng không biến thành 0. Snapshot partial có thể cung cấp observation hợp lệ nhưng được gắn cờ; mục vắng trong snapshot partial không được kết luận là `NOT_IN_TOP`.
- `max_views_ceil` là giá trị lớn nhất hợp lệ không âm trong **toàn bộ keyword sau cleaning của cùng source, country, access và ngày phân tích**, bao gồm cả keyword thiếu baseline, không lọc theo category; được ghi trong `score_components`. Với current > 0 và baseline > 0: `G = clip(log2(current / baseline) / 3, 0, 1)`, `V = log1p(current) / log1p(max_views_ceil)`, `score = round(100 * (0.7 * G + 0.3 * V))` giới hạn 0–100. `round` dùng quy tắc Python ties-to-even.
- `EMERGING` nếu ratio >= 1.5 và score >= 50; `FALLING` nếu ratio < 0.75; các trường hợp hợp lệ còn lại là `STABLE`. Nếu không đủ dữ liệu hợp lệ, score/ratio là `None` và status `INSUFFICIENT_DATA`.
- Kết quả lưu các ngày quan sát và giá trị baseline, ngày thiếu cùng lý do, score components, version `wikimedia-trend-v0.1`, source scope, link và quality flags. `CENSORED_TOP_LIST`, `VIEWS_APPROXIMATE`, `INCOMPLETE_BASELINE_COVERAGE` phản ánh giới hạn nguồn. Median có điều kiện theo các ngày lọt top nên có thể lệch. Chấm điểm không phải xác suất hay bằng chứng trend social hoặc nhu cầu mua hàng.

```bash
python -m unittest discover -s tests -v
python -m scripts.wikimedia_trend_smoke
```

Smoke gọi lại API thật, tính sau cleaning và classification; so sánh top 10 score với top 10 lượt xem trong nhóm có thể tính score, đồng thời in top volume toàn bộ gồm cả mục thiếu baseline. Xếp hạng score theo score giảm, ratio giảm, views giảm, rồi project/title; volume theo views giảm, project/title. Không lưu Firestore hay cung cấp API kết quả. Namespace bản địa hóa có thể còn lọt qua cleaning và ảnh hưởng score/mẫu số; cần kiểm tra trước khi diễn giải kết quả.

## Bước 8 — API run thủ công

API điều phối adapter → cleaning → keyword → scoring → storage. Chạy trên **một máy Ubuntu**; các process trên cùng máy phải dùng chung `TREND_SCOUT_LOCK_PATH`. Khóa `flock` và cooldown ngăn các run đồng thời trên cùng host; đây không phải khóa phân tán và chưa hỗ trợ chạy trên nhiều host/container. Không dùng cấu hình này để triển khai nhiều instance.

Cấu hình trước khi khởi động (ADC đã đăng nhập bằng `gcloud auth application-default login`):

```bash
export GOOGLE_CLOUD_PROJECT="trendscout-192e4"
# Tạo token ngẫu nhiên trong shell; không ghi vào repository hoặc gửi qua chat.
export TREND_SCOUT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Giữ token trong shell của người vận hành, tối thiểu 24 ký tự. Nếu thiếu token, POST bị vô hiệu hóa; GET không yêu cầu token. Mặc định server bind localhost; nếu cho phép truy cập qua mạng cần bổ sung TLS, kiểm soát mạng và quản lý token trước. `TREND_SCOUT_RUN_COOLDOWN_SECONDS` mặc định 60; `TREND_SCOUT_LOCK_PATH` mặc định `~/.cache/trendscout/manual-run.lock`. Không xóa file khóa khi server đang hoạt động; mọi process phải cùng một đường dẫn. Nếu worker bị gián đoạn, lần khởi động tiếp theo đánh dấu run dang dở `ABORTED` khi lấy được khóa và kết nối storage.

Ở terminal có token trong môi trường:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/runs \
  -H "Authorization: Bearer $TREND_SCOUT_OPERATOR_TOKEN"
# Ví dụ response: {"run_id":"...", "status":"QUEUED"}
export TREND_SCOUT_RUN_ID="<run_id từ response>"
curl -sS "http://127.0.0.1:8000/api/runs/$TREND_SCOUT_RUN_ID"
curl -sS "http://127.0.0.1:8000/api/runs/$TREND_SCOUT_RUN_ID/keywords?category=all&sort=score&limit=10&offset=0"
curl -sS -G "http://127.0.0.1:8000/api/runs/$TREND_SCOUT_RUN_ID/keywords" --data-urlencode 'q=fashion' --data-urlencode 'category=unclassified'
curl -sS "http://127.0.0.1:8000/api/runs/$TREND_SCOUT_RUN_ID/keywords/<keyword_id>"
```

POST chỉ chạy thủ công; đang chạy trả 409, cooldown trả 429, sai token trả 401. Trạng thái gồm `QUEUED`, `FETCHING`, `NORMALIZING`, `ANALYZING`, `PERSISTING`, `COMPLETED`, `PARTIAL`, `FAILED`, `ABORTED`. `COMPLETED` chỉ được ghi sau khi các document cần thiết được lưu; `PARTIAL` có thể là lịch sử nguồn thiếu hoặc lỗi giữa lúc lưu. Nếu chưa lưu đủ keyword, API kết quả trả 409. Nếu Firestore không thể ghi cả trạng thái lỗi, marker trên host cho phép đánh dấu `ABORTED` khi phục hồi; không có cam kết phục hồi khi file khóa bị xóa hoặc host mất dữ liệu.

Danh sách hỗ trợ `q` tìm trong kết quả đã lưu, `category=all|fashion|home_goods|unclassified`, `sort=score|views|keyword`, `limit` 1–100 và `offset`. Response gồm `items`, `total`, `offset`, `limit`, `next_offset`, `empty_reason`; nhóm không có kết quả trả `NO_MATCHES_IN_ANALYZED_DATA`. Truy vấn chỉ đọc Firestore; không fetch Wikimedia. Hiện mỗi lần tìm đọc tối đa 16 shard và lọc/sắp xếp trong bộ nhớ: phù hợp demo, không phù hợp dữ liệu lớn hoặc truy vấn tần suất cao. `offset` không nên dùng với dữ liệu thay đổi; run kết thúc hiện có tập kết quả cố định.

Firestore `(default)` lưu tại `analysis_runs/{run_id}` với các subcollection:

- `snapshots/{YYYY-MM-DD}`: metadata fetch, HTTP status, lỗi và báo cáo cleaning.
- `observations/{YYYY-MM-DD}-{chunk}`: tối đa 40 observation đã cleaning và provenance mỗi document, gồm dữ liệu lịch sử cho baseline.
- `keyword_results/shard-{00..15}`: danh sách kết quả ngày phân tích, classification, score components, baseline và evidence, chia theo ký tự đầu của SHA-256 keyword ID.

IDs ổn định trong mỗi run; ghi lại cùng ID thay thế nội dung. Giới hạn mỗi document theo kích thước JSON 700 KB trước khi gửi, lỗi được báo thay vì âm thầm thiếu. Không triển khai xóa run; xóa document cha bằng Firestore không xóa subcollection. Một run thực tế gần 8.000 observation dự kiến ~226 lần ghi snapshot/bucket cộng cập nhật trạng thái, nhưng usage và quota thực tế phải kiểm tra trước khi chạy. Theo [Firestore quotas](https://firebase.google.com/docs/firestore/quotas), free tier có 20.000 write và 50.000 read/ngày; giới hạn và mức sử dụng thực tế có thể thay đổi. Cloud Monitoring trên project Spark hiện trả 403 yêu cầu billing, do đó không thể suy ra quota còn lại từ lần kiểm tra này; kiểm tra Usage trong Firebase Console trước run live. Không bật billing chỉ để kiểm tra.

Chạy test offline `python -m unittest discover -s tests -v`. Fake repository không kiểm chứng lưu bền Firestore; cần run live và đọc lại sau restart để nghiệm thu end-to-end. Không có job nền tự động hay frontend trong bước này.

## Dashboard local

FastAPI phục vụ giao diện tại `http://127.0.0.1:8000/dashboard/` cùng origin với API; không cần mở CORS. Thao tác, cấu hình operator token và các giới hạn kiểm thử được mô tả trong [Frontend README](../Frontend/README.md). Dashboard không tự tạo run khi mở hoặc reload.

## D3A — Public Fetch ngoại tuyến

### D5: demo local không cần tài khoản

Chạy `bash demo.sh` tại thư mục gốc, sau đó mở `http://127.0.0.1:8000/dashboard/` và nhấn Fetch. Nếu cổng 8000 đã dùng, chọn cổng khác bằng `TREND_SCOUT_DEMO_PORT=18765 bash demo.sh`. Ứng dụng bind loopback, public admission/queue/checkpoint dùng SQLite tạm và legacy run data dùng `FakeStorage`; mọi fixture được gắn `SYNTHETIC`, dữ liệu mất khi dừng. Không cần Firebase, Firestore, API key hay token. Public Fetch đi qua admission, queue, worker và discovery cho cả bốn source IDs, nhưng handlers chỉ trả fixture offline (không gọi Wikimedia, SerpApi hay YouTube). Kết quả được gắn `SYNTHETIC`, source delivery `FRESH`, và candidate snapshot là `INSUFFICIENT_EVIDENCE`; không cộng rank với lượt xem hoặc tuyên bố trend thật. Public Fetch trả tối đa 100 candidates từ tối đa 10.000 observations; tìm kiếm/bộ lọc trên dashboard chỉ áp dụng phần kết quả được trả. `bash start.sh` là runtime cloud cũ và cần cấu hình project/token riêng; nó không còn mặc định một cloud project. Không triển khai demo local ra Internet hoặc coi SQLite local là kiểm soát phân tán production.

### D5: demo-first Fetch và production gate riêng

`DEMO_ACCEPTANCE=100%` được xác minh cho dashboard local với one-click Fetch, bốn handler fixture cố định gắn nhãn `SYNTHETIC`, keyword discovery/search/filter/detail, UI regression và demo-scope security. Chạy bằng `bash demo.sh`; không cần tài khoản, API key, Firebase/Firestore hoặc dịch vụ trả phí và không gọi provider thật. Demo local không phải production deployment.

`app/sources/live_gateway.py` và `guarded_collector.py` có boundary riêng cho Google/Bing qua SerpApi và YouTube; `D5SourceHandlers` nối ba collector này cùng Wikimedia fixed-date gateway vào fenced worker, synthetic-only cache và `DistributedPublicFetch`. `create_app` chỉ cho phép facade injection trong local/test synthetic mode với feature flag bật tường minh và đủ bốn handler SYNTHETIC; route legacy `POST /api/runs` vẫn yêu cầu Bearer token. `LocalSQLiteAtomicStateStore` là backing file-based thật cho local/test synthetic mode; API integration đi qua shared admission, fenced worker, bốn MockTransport adapters và cache. Offline tests xác minh multi-process admission/quota, idempotency, lease/fencing, cache và tối đa một execution cho một job. `SourceCheckpointJournal` ghi response receipt trước khi settle attempt; crash/recovery tests cho cả bốn source tiếp tục từ receipt đã commit mà không gửi lại provider request. Local quota ledger prune bucket UTC đã qua trong transaction, giữ unknown/malformed/future state để fail closed.

`DistributedWorkerRunner` cung cấp polling loop có graceful shutdown và chuyển lỗi lên process supervisor. Synthetic Firestore multi-host probe, systemd runbook và offline checks đã có, nhưng **chưa được chạy trên hai host thực**. `PRODUCTION_MULTI_HOST_ACCEPTANCE=BLOCKED`; cùng máy hoặc SQLite không thay thế shared backing, hai host độc lập và supervisor recovery. `D5_PRODUCTION_READINESS=PARTIAL`, `FOUR_SOURCE_LIVE_VERIFIED=NOT_ACCEPTED`; public live/provider calls tiếp tục disabled. Không dùng Firebase cá nhân hoặc bật production Fetch. Xem [D5 acceptance matrix](../Documents/Design/D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md), [kiến trúc D5](../Documents/Design/D5_LIVE_MULTI_SOURCE_SYSTEM_ARCHITECTURE.md) và [multi-host runbook](../Documents/Design/D5_MULTI_HOST_TEST_AND_DEPLOYMENT_RUNBOOK.md).

Website không có luồng đăng nhập, đăng xuất, đăng ký hoặc tài khoản người dùng. `GET /api/v1/capabilities` đọc công khai trạng thái runtime và bốn mô tả nguồn; response không chứa credential. `POST /api/v1/fetches` là endpoint riêng, không thay đổi `POST /api/runs` (endpoint cũ vẫn yêu cầu Bearer operator token). Request public chỉ nhận JSON `{}`; backend chọn plan, nguồn và ngân sách; client không thể truyền URL/query/provider/budget. Ngoài runtime local legacy, `create_app` có thể nhận injected `DistributedPublicFetch` chỉ trong local/test synthetic mode, với policy bật tường minh, đủ bốn handler khai báo SYNTHETIC và `AtomicStateStore` do harness cung cấp. API worker path không cần Firebase/FakeStorage, nhưng không tự tạo backing và không được bật trên production/Vercel.

Public Fetch mặc định tắt qua `TREND_SCOUT_PUBLIC_FETCH_ENABLED=false`. Legacy runtime cần `FakeStorage` và adapter fixture. Worker facade dùng atomic admission/idempotency/quota/poll budgets và handler cố định, nhưng chỉ được inject trong profile `local`/`test`, mode `synthetic`, không read-only, không project cloud và flag bật rõ ràng. `LocalSQLiteAtomicStateStore` từ chối production/live/memory DB, giới hạn transaction trên một host/local filesystem, và không chứng minh backing đa host. Cả hai đường đều giữ body tối đa 1 KiB và không nhận query/provider/budget từ client.

Các giới hạn này chỉ xác nhận luồng synthetic trong môi trường local/test cô lập. Chúng **không** bảo vệ giữa nhiều host/container độc lập, không chống bot phân tán qua quota bền vững và không đủ để mở public live Fetch. Origin/Host/Fetch Metadata là tín hiệu chống lạm dụng trình duyệt, không xác thực client; client HTTP trực tiếp vẫn gọi được endpoint nếu deployment bật. Vì vậy production/Vercel, Wikimedia live, YouTube, Google Search, Bing Search và mọi provider tính phí đều bị khóa. Không có yêu cầu hoặc thao tác Firestore trong D3A.

Chạy kiểm chứng synthetic offline bằng `bash Backend/scripts/verify_offline_readiness.sh`. Test tạo app với fixture adapter và fake storage; không cần bật flag trong shell, gọi provider thật, Firestore hoặc emulator. Public live chỉ được xem xét sau khi có phê duyệt riêng và xác minh distributed rate/concurrency/idempotency, durable quota/cost budgets, bot/abuse controls, monitoring và provider approvals.

## D3B — YouTube search adapter ngoại tuyến

`app/sources/youtube/` triển khai adapter query-based riêng cho YouTube Data API v3 `search.list`; không ép video search resource vào `PageObservation`/Wikimedia scoring. Adapter dùng fixed endpoint và allowlist tham số từ plan server-side `demo-us-en` v1, deduplicate theo provider/video ID nhưng giữ từng query evidence, và chỉ tạo descriptor `result_rank` (vị trí trong query response), không tạo views, trend score hay demand score.

Implementation được cô lập ở chế độ offline: constructor chỉ nhận `httpx.MockTransport`; credential boundary đọc tham chiếu server-side `YOUTUBE_API_KEY` và gửi giá trị qua header `X-Goog-Api-Key`, không đưa key vào URL. Giai đoạn này không có resolver môi trường, endpoint live, registry factory hay đường gọi từ Public Fetch; fixtures là dữ liệu synthetic. Quota tracker đếm từng attempt/retry, nhưng không suy ra quota còn lại của Google Cloud project.

Default plan giữ nguyên 14 query và một page/query. Cursor do provider trả về được gắn với query/plan/filter/cutoff context; page cap, request budget, retry wait, deadline, cancellation, malformed body, quota và rate-limit đều có stop/error state riêng. Request/response body không được lưu. YouTube vẫn `PENDING_APPROVAL`, `NOT_CONFIGURED`, `NOT_VERIFIED`, `default_enabled=false`; không được dùng như nguồn live hoặc trong multi-source scoring.

Chạy adapter contract tests riêng:

```bash
cd Backend
.venv/bin/python -m unittest discover -s tests -p 'test_youtube_adapter.py' -v
```

Các kiểm tra tích hợp/regression offline nằm trong `bash Backend/scripts/verify_offline_readiness.sh`. Tham khảo [báo cáo D3B](../Documents/Reports/BAO_CAO_DEMO_D3B.md) và [source capability matrix](../Documents/Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md). Tài liệu Google được đối chiếu ngày 11/11/2026; quota của project thật và quyền dùng/lưu dữ liệu chưa được xác minh hoặc phê duyệt.

### Ngân sách kiểm thử Bước 10

Trước khi ghi bucket, backend ước lượng số document writes (snapshot + observation chunks + 16 shard kết quả + 10 lượt dự phòng trạng thái) và dừng nếu vượt 1.000. Đây là guard số thao tác dự kiến, không phải đồng hồ quota Firestore chính xác; retry, ghi từ process khác và độ trễ hiển thị usage có thể ảnh hưởng con số thực tế. Các test có thể truyền `run_mode="synthetic"` hoặc `"replay"` vào `create_app` khi inject nguồn giả, để dữ liệu test không được gắn nhãn live. Production mặc định live và không cho client chọn mode.

## Hotfix dashboard công khai và runtime chỉ đọc trên Vercel

**Trạng thái code:** hotfix mới chỉ được kiểm tra offline; chưa xác minh deployment Vercel mới. Deployment `dpl_EZbtefvf2PqprB7bgrUtnJTStTMH` đã được kiểm tra ở trạng thái READY nhưng runtime trả HTTP 500 cho `/`, `/dashboard/` và `/api/health`; runtime log báo thiếu `fastapi`. Build log chọn `pyproject.toml`, trước hotfix file này không khai báo runtime dependencies, còn `requirements.txt` không được cài. Bản sửa khai báo toàn bộ runtime package đang pin trong `[project].dependencies`; `requirements.txt` chuyển tiếp về project metadata để chỉ có một danh sách pin. `requires-python` giới hạn deployment ở Python 3.12. Tài liệu Vercel xác nhận Python runtime đọc dependencies và khoảng phiên bản interpreter từ các trường này ([Python runtime](https://vercel.com/docs/functions/runtimes/python), [FastAPI trên Vercel](https://vercel.com/docs/frameworks/backend/fastapi); đã kiểm tra 11/11/2026).

`./start.sh` local không đổi. Trên Vercel (`VERCEL=1`), ứng dụng vẫn chỉ đọc: không tạo `RunService`/`HostRunLock`, không recovery run và không chạy pipeline. GET `/` redirect sang `/dashboard/`; HTML, CSS, JavaScript modules, `/api/health` và `/api/v1/capabilities` công khai ở tầng ứng dụng. GET `/api/runs/{run_id}` và các route kết quả vẫn riêng tư, cần HTTP Basic (`viewer` cùng `TREND_SCOUT_READ_PASSWORD` dài ít nhất 32 ký tự). Nếu thiếu secret này, đọc run bị chặn với 503; dashboard công khai không phụ thuộc vào nó. Đọc run thành công còn cần Vercel OIDC/Firestore identity được cấu hình. Ứng dụng không mở rộng quyền Firestore. POST `/api/runs` và POST `/api/v1/fetches` trả 405. Không đưa operator token vào demo online.

Theo thông tin đã xác minh, Vercel project đang bật Deployment Protection `all_except_custom_domains`, lần kiểm tra không trả về Production environment variables nào, và domain `trend-scout-mu.vercel.app` đã verified. Hotfix không thay đổi Deployment Protection. Cơ chế bảo vệ ở nền tảng có thể chặn request trước khi tới FastAPI; cần kiểm tra riêng hành vi route anonymous trên deployment/domain mới. Chưa được kết luận dashboard truy cập công khai cho đến khi bước kiểm tra đó đạt.

1. Vercel project setup remains Root Directory `Backend`, Framework Preset FastAPI, with **Include source files outside of the Root Directory in the Build Step** enabled. Keep `Backend/vercel.json`'s existing build command and `app.main:app` entrypoint. The build command packages every non-hidden asset under `Frontend/public` into `app/dashboard_assets.py`, including nested ES modules; do not move the dashboard to a CDN path outside the application's route boundary. Runtime dependencies come from `Backend/pyproject.toml`; local setup through `requirements.txt` installs the same project dependencies. Python is constrained to 3.12.
2. Chỉ thực hiện nếu cần private run reads. Bật Secure Backend Access / OIDC cho Vercel project, chọn team issuer. Trong Google Cloud project hiện hữu tạo Workload Identity Pool và OIDC provider theo [hướng dẫn Vercel](https://vercel.com/docs/oidc/gcp); cấu hình issuer, allowed audience đúng với token Vercel, attribute mapping và **attribute condition giới hạn chính xác team, project `trend-scout` và environment production**. Xác minh các claim thực tế theo [OIDC reference](https://vercel.com/docs/oidc/reference); không cấp quyền cho toàn bộ Vercel hoặc toàn pool. Không dùng key JSON, ADC Ubuntu hoặc service-account key. Cấu hình này không cần thiết để hiển thị dashboard công khai.
3. Chỉ thực hiện nếu cần private run reads. Cấp trực tiếp cho principal/principalSet đã giới hạn của pool role Firestore chỉ đọc `roles/datastore.viewer` ở project `trendscout-192e4` theo [Firestore IAM](https://cloud.google.com/firestore/native/docs/security/iam). Role này có phạm vi rộng hơn các collection demo: xem chính xác quyền và dùng project demo riêng nếu cần cô lập dữ liệu. Không cấp Editor, Owner hoặc Datastore User. Admin SDK/server dùng IAM, không dựa vào Firestore Security Rules. Không thay đổi billing. Các bước IAM là thao tác quản trị cần thực hiện riêng; chưa được kiểm chứng live.
4. Chỉ khi cần mở private run reads, cấu hình có chủ đích `GOOGLE_CLOUD_PROJECT`, `GOOGLE_WIF_AUDIENCE` (resource name dạng `//iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER`) và `TREND_SCOUT_READ_PASSWORD` trong Production. Không cần `TREND_SCOUT_READ_PASSWORD` để render dashboard hoặc đọc capability. `VERCEL` và OIDC request header do Vercel cung cấp; không tự điền hoặc lưu OIDC token. Không dùng ADC hay service-account key. Tránh cấp WIF cho preview nếu không cần.
5. Sau deploy mới: trước hết kiểm tra Deployment Protection/domain riêng, rồi xác nhận `/` trả redirect, dashboard HTML/CSS/JS và capability tải được không cần app password, còn run GET bị chặn khi không có app password và POST `/api/runs` + POST `/api/v1/fetches` trả 405. Nếu private reads được cấu hình, kiểm tra bằng một run ID đã biết và xác nhận OIDC/IAM chỉ cho phép đọc. 503 cho thấy private read chưa cấu hình hoặc Firestore không truy cập được. Không coi local tests/build log là xác minh production; đọc logs mà không chia sẻ token. Đọc qua Vercel có thể tiêu tốn Firestore reads.

[Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi) · [OIDC trên mọi plan](https://vercel.com/docs/oidc) · [Chia sẻ source ngoài root](https://vercel.com/docs/monorepos/monorepo-faq).
