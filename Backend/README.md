# TrendScout backend — Bước 3

## Vercel shared Collections snapshot format

The shared SQLite image is stored as a gzip stream in one or more bounded,
content-addressed Firestore blobs. Each blob is committed independently; the
versioned pointer is changed only after all blobs are stored. Readers verify the
complete SQLite SHA-256 before opening the working copy. Existing single-blob
`snapshot_ref` pointers remain readable. Multi-blob pointers use `snapshot_refs`;
releases predating this format cannot read them, so a rollback after the first
multi-blob write must use a release with multi-blob reader support. This change
does not reset quota or replace the shared database. Unreferenced blobs from an
interrupted upload follow the existing blob retention and cleanup policy.

## Native Windows (môi trường chính)

Yêu cầu Python 3.12 native, Git, Node.js và Chrome. Từ repository root chạy `.\setup.ps1`; dependency cài vào `Backend\.venv`, không cài global. Python Launcher phải có bản 3.12; nếu chưa có, cài chính thức bằng `winget install --id Python.Python.3.12 --exact`.

```powershell
..\setup.ps1
..\start.ps1
```

Windows `start.ps1` khởi chạy profile LIVE đa nguồn trên loopback với một worker: Wikimedia, Google Trends/Google Search, Bing Search và YouTube Search. Google/Bing cần `SERPAPI_API_KEY`, YouTube cần `YOUTUBE_API_KEY` trong `.env` ở repository root. Khởi động không gọi provider; chọn Fetch trong dashboard mới bắt đầu thu thập. Quota SerpApi, lịch YouTube và đồng ý chính sách YouTube vẫn được kiểm tra. Không cần Firebase, Google Cloud project, operator token hoặc ADC. Cổng mặc định là 8000; có thể đặt `TREND_SCOUT_DEMO_PORT` trong environment. Nếu Windows chặn script cục bộ, thiết lập một lần cho tài khoản hiện tại bằng `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Setup không tạo file `.env` hoặc API keys. `LocalCredentialProvider` đọc `.env` ở repository root; Windows vẫn hỗ trợ `%LOCALAPPDATA%\TrendScout\.env` cũ nếu file root chưa tồn tại.

Chạy `..\verify_offline_readiness.ps1` để thực hiện D6.1B focused tests, toàn bộ unittest, cả hai golden hash seed, `pip check`, JavaScript syntax, Chrome browser E2E và storage benchmark. File lock dùng byte-range lock Windows qua `msvcrt`, tự nhả khi process kết thúc. Kiểm tra ACL xác minh owner SID và DACL bằng Windows security APIs; cho phép user hiện tại, SYSTEM, Administrators và owner-rights ACE. Reparse point, ACL không đọc được hoặc ACE cấp quyền cho principal khác bị từ chối. SQLite quota ledger nằm dưới `%LOCALAPPDATA%\TrendScout\local\d6-smoke`; database, WAL/SHM và process lock thừa hưởng ACL riêng của thư mục đó.

## D4 — Google/Bing SERP ngoại tuyến

`app/sources/serpapi/` là hợp đồng dùng chung cho Google và Bing qua SerpApi candidate; hai adapter chỉ nhận `httpx.MockTransport`, fake resolver và quota pool được inject. Request dùng query thuộc `demo-us-en` v1, endpoint cố định, Google `start` và Bing `first`; provider next URL chỉ được phân tích offset, không được gọi. Mỗi attempt reserve budget, retry/deadline/page cap có giới hạn; record deduplicate URL nhưng giữ query evidence, trạng thái cache và thời điểm provider nếu hợp lệ. Position là vị trí trong mẫu SERP, không phải search volume hay sales. Không có filter bảy ngày khi ngữ nghĩa chưa được xác minh.

Quota pool D4 chỉ process-local, không bảo vệ nhiều host/restart; không được dùng để mở public live Fetch. Cả hai source vẫn disabled, chưa cấu hình, chưa phê duyệt hoặc xác minh live; chúng không được thêm vào registry/Public Fetch. [Capability matrix](../Documents/Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md), [inventory](../Documents/Design/US_PLATFORM_SOURCE_INVENTORY.md), [D5 blueprint](../Documents/Design/D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md).

Để cài đặt và khởi động từ Windows PowerShell tại repository root, chạy `.\setup.ps1` một lần rồi `.\start.ps1`; xem [README chính](../README.md). `start.ps1` chạy profile LIVE đa nguồn đã mô tả ở trên; các runtime legacy bên dưới tách biệt với launcher này.

Quy tắc không cần Firebase/ADC ở trên áp dụng cho SQLite mặc định. Để chọn Shared LIVE bằng Firestore hiện có cho demo local, chạy `..\start.ps1 -FirestoreSharedLive`. Chế độ này chỉ chạy loopback, pin `trendscout-192e4/(default)` và chỉ chấp nhận ADC impersonate `trendscout-vercel-backend@trendscout-192e4.iam.gserviceaccount.com`; không chấp nhận service-account key. Quota, cache, checkpoint và snapshot dùng database chung; Fetch vẫn đi qua approval/quota guards hiện có. Bỏ switch để rollback về SQLite. Vercel Shared LIVE có feature flag riêng, mặc định tắt, mô tả bên dưới.

### Vercel Shared LIVE — writable multi-source demo

**Continuation response contract:** A Vercel Shared LIVE `POST /api/v1/fetches` with the continuation header includes `continuation_ready: true` only when the completed worker slice returned the same durable job to the queue. The browser can then dispatch the next same-key slice directly. Deferred, concurrent, terminal, or uncertain work continues through the status endpoint.

`TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED` defaults to `false`. When enabled on Vercel, the application reports `read_only=false` for its supported demo APIs but does not start the desktop SQLite `RunService`, lock, or recovery path. Interactive API documentation stays disabled. Request-scoped Vercel OIDC is exchanged through the pinned WIF provider and existing backend service account; credentials stay server-side. Shared LIVE Fetch supports the owner-approved Wikimedia, Google, Bing, and YouTube source set behind the existing consent, quota, refresh, and provider approval guards. Collections endpoints restore their bounded SQLite working copy from a versioned snapshot in `trendscout-192e4/(default)` and commit mutations to that database before returning success; concurrent stale snapshots fail closed instead of overwriting newer data. On Vercel, the first Fetch POST admits the durable job and returns its ID before provider work. Each same-key continuation runs at most one source slice. A normal queued slice response sets `continuation_ready: true`, so the browser can continue directly without a status GET. After a terminal result, the browser reads the source outcomes and posts a cache-only Collections recovery request; the dashboard waits for the persisted snapshot before showing completion. If a function or lease expires, later continuation resumes from durable source checkpoints; missing source checkpoints cannot be reported as a completed job. Vercel Google, Bing, and YouTube manual Fetch requires the configured `TREND_SCOUT_OPERATOR_TOKEN`. One owner-approved first Fetch with all four sources may bypass the 8-hour source spacing and daily application caps; its bounded provider attempts are still recorded in the shared Firestore ledger, monthly limits remain enforced, and the provider APIs still apply their own quotas. Subsequent manual Fetches return fresh shared cache entries before making provider requests. Each Bing provider call reserves the shared Firestore provider and Bing allocation budgets atomically in `GuardedAttemptRunner`; Vercel Google and Bing do not depend on a separate live SerpApi Account API read before those reservations. Bing receives the completed same-job Google checkpoint before Collections processing. Google SERP verification remains limited to its existing two selected candidates per batch. YouTube retains up to 50 normalized records when they fit the configured 3.5 MB result/checkpoint cap, and oversized checkpoint records use the existing snapshot blob store. The YouTube consent policy remains `d6-youtube-v2`. Terminal jobs can replay Collections from retained LIVE checkpoints without running source adapters again. The dashboard retries this cache-only replay after a terminal Fetch and on page load when a terminal LIVE Fetch has no saved dataset. This can write a recovered dataset snapshot, but it does not submit a new Fetch or contact source providers. Snapshot failures return a sanitized persistence error so the idempotent request can be retried. The legacy process-backed `POST /api/runs` path is not used on Vercel. Turning the feature flag off disables the shared runtime; no IAM or billing change is part of this setting.

Dashboard files are staged into `Backend/public/dashboard` during the Vercel build and served through FastAPI `StaticFiles`; `[tool.vercel.fastapi.static]` forces CDN promotion and excludes those assets from the function bundle. The `/` to `/dashboard/` redirect is handled by Vercel before the Python function. The interactive app remains same-origin with the API. Vercel added FastAPI static-file CDN promotion in September 2026; configuration was checked against the official docs on 2026-11-23 ([FastAPI static files on Vercel](https://vercel.com/changelog/fastapi-frontends-and-static-files-served-from-the-cdn), [Vercel redirect configuration](https://vercel.com/docs/project-configuration/vercel-json)).
Function dùng `maxDuration: 120` trong `vercel.json`. Theo tài liệu Vercel hiện tại, Hobby hỗ trợ tối đa 300 giây khi Fluid Compute bật và 60 giây khi tắt; giá trị 120 giây đã được Production chấp nhận ([Vercel Function duration](https://vercel.com/docs/functions/configuring-functions/duration), kiểm tra 2026-11-23). Preview không được production subject cho phép impersonate nên Preview không chứng minh Production identity. Deployment mới và Fetch LIVE thật vẫn cần được owner chủ động duyệt; không dùng lần kiểm tra Preview để tuyên bố Production identity hoặc bốn nguồn đã đạt FRESH.

Repository còn có legacy Firestore-backed run API riêng, tách biệt với Shared LIVE Fetch. Chỉ theo hướng dẫn bên dưới khi làm việc riêng với runtime legacy.

## Firestore và ADC

Đối với project đã có và database `(default)`:

```powershell
gcloud auth application-default login
$env:GOOGLE_CLOUD_PROJECT = 'trendscout-192e4'
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

```powershell
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
    .\.venv\Scripts\python.exe -m pip check
} finally {
    Pop-Location
}
```

Các test dùng fake và mock, không gọi cloud. Smoke script ghi một document tạm rồi xóa nó. Script đã bị khóa với project production `trendscout-192e4` và project number `556422620769`; không dùng nó để kiểm tra production. Hiện chưa có shared staging project được phê duyệt, vì vậy không chạy smoke live trong acceptance này.

```powershell
$env:GOOGLE_CLOUD_PROJECT = 'YOUR_APPROVED_NONPRODUCTION_PROJECT_ID'
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m scripts.firestore_smoke `
        --confirm-nonproduction-project $env:GOOGLE_CLOUD_PROJECT `
        --acknowledge-nonproduction-writes
} finally {
    Pop-Location
}
```

Chỉ thay placeholder sau khi owner xác nhận đây là project non-production đã tồn tại và cho phép ghi synthetic data. Script yêu cầu project ID trong `GOOGLE_CLOUD_PROJECT` khớp với cờ xác nhận, cờ `--acknowledge-nonproduction-writes`, và từ chối cấu hình emulator. Nó tạo, đọc, cập nhật rồi xóa document `analysis_runs/step3-smoke-*`; nếu tiến trình bị gián đoạn, document tạm có thể còn lại. Không bật billing hoặc tạo project/database để chạy smoke này.

## Ranh giới source

- `app/api/`: health route; không có API run.
- `app/adapters/`, `app/models/`: interface nguồn và model dự kiến.
- `app/storage/`: contract, Firestore và fake.
- `app/processing/`, `app/analysis/`, `app/orchestration/`: vị trí dành cho các bước sau.

## Bước 4 — Wikimedia top-per-country

Adapter `app/adapters/wikimedia.py` gọi danh sách `US/all-access` không có query lọc chủ đề/ngôn ngữ. Mặc định bắt đầu từ hôm qua theo UTC, thử lùi tối đa 7 ngày khi gặp 404, rồi lấy 7 ngày lịch trước ngày phân tích. Request gửi tuần tự trong từng adapter; không chạy nhiều adapter song song để vượt chính sách nguồn. Lỗi khác 404 khi tìm ngày mới nhất làm dừng tìm kiếm, không âm thầm dùng ngày cũ. Lịch sử có thể thiếu; tổng số request gồm cả retry. Không tự ghi Firestore.

```powershell
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m scripts.wikimedia_smoke
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
} finally {
    Pop-Location
}
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

```powershell
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
    .\.venv\Scripts\python.exe -m scripts.wikimedia_cleaning_smoke
} finally {
    Pop-Location
}
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

```powershell
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
    .\.venv\Scripts\python.exe -m scripts.wikimedia_keywords_smoke
} finally {
    Pop-Location
}
```

Script smoke gọi lại Wikimedia, làm sạch rồi phân loại; in số observation và số entity riêng cùng ví dụ, không ghi Firestore. Một entity có thể có nhiều observation theo ngày; số nhóm có thể chồng lấn khi có nhiều nhãn. Nếu nguồn trả thiếu ngày hoặc lỗi, số liệu chỉ phản ánh các snapshot lấy được.

## Bước 7 — Scoring Wikimedia v0.1

`app/analysis/trend.py` tính heuristic cho các keyword đã qua cleaning trong ngày phân tích. Input là `DayData` cho từng snapshot: snapshot gốc, keyword sau cleaning và các row adapter trước cleaning. Scorer nhóm theo `(source, country_scope, access, project, article_original)` và chỉ dùng profile Wikimedia; không trộn nguồn, project hay phạm vi.

- Baseline là median của tối đa 7 ngày lịch trước đó mà trang có mặt và có `views_ceil` hợp lệ; cần ít nhất 3 ngày quan sát. Giá trị 0 hợp lệ là một quan sát, nhưng current hoặc median bằng 0 không được chia và trả `INSUFFICIENT_DATA`.
- Các ngày thiếu được giữ cùng lý do: `DAY_UNAVAILABLE`, `FETCH_ERROR`, `SNAPSHOT_MISSING`, `NOT_IN_TOP`, `PARTIAL_SNAPSHOT`, `REJECTED_BY_CLEANING`, `INVALID_VALUE`, `DUPLICATE_OBSERVATION`. Chúng không biến thành 0. Snapshot partial có thể cung cấp observation hợp lệ nhưng được gắn cờ; mục vắng trong snapshot partial không được kết luận là `NOT_IN_TOP`.
- `max_views_ceil` là giá trị lớn nhất hợp lệ không âm trong **toàn bộ keyword sau cleaning của cùng source, country, access và ngày phân tích**, bao gồm cả keyword thiếu baseline, không lọc theo category; được ghi trong `score_components`. Với current > 0 và baseline > 0: `G = clip(log2(current / baseline) / 3, 0, 1)`, `V = log1p(current) / log1p(max_views_ceil)`, `score = round(100 * (0.7 * G + 0.3 * V))` giới hạn 0–100. `round` dùng quy tắc Python ties-to-even.
- `EMERGING` nếu ratio >= 1.5 và score >= 50; `FALLING` nếu ratio < 0.75; các trường hợp hợp lệ còn lại là `STABLE`. Nếu không đủ dữ liệu hợp lệ, score/ratio là `None` và status `INSUFFICIENT_DATA`.
- Kết quả lưu các ngày quan sát và giá trị baseline, ngày thiếu cùng lý do, score components, version `wikimedia-trend-v0.1`, source scope, link và quality flags. `CENSORED_TOP_LIST`, `VIEWS_APPROXIMATE`, `INCOMPLETE_BASELINE_COVERAGE` phản ánh giới hạn nguồn. Median có điều kiện theo các ngày lọt top nên có thể lệch. Chấm điểm không phải xác suất hay bằng chứng trend social hoặc nhu cầu mua hàng.

```powershell
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
    .\.venv\Scripts\python.exe -m scripts.wikimedia_trend_smoke
} finally {
    Pop-Location
}
```

Smoke gọi lại API thật, tính sau cleaning và classification; so sánh top 10 score với top 10 lượt xem trong nhóm có thể tính score, đồng thời in top volume toàn bộ gồm cả mục thiếu baseline. Xếp hạng score theo score giảm, ratio giảm, views giảm, rồi project/title; volume theo views giảm, project/title. Không lưu Firestore hay cung cấp API kết quả. Namespace bản địa hóa có thể còn lọt qua cleaning và ảnh hưởng score/mẫu số; cần kiểm tra trước khi diễn giải kết quả.

## Bước 8 — API run thủ công

API điều phối adapter → cleaning → keyword → scoring → storage. Runtime legacy này chỉ được cấu hình cho **một máy**; các process trên cùng máy phải dùng chung `TREND_SCOUT_LOCK_PATH`. Khóa cục bộ và cooldown ngăn các run đồng thời trên cùng host; đây không phải khóa phân tán và chưa hỗ trợ nhiều host/container. Không dùng cấu hình này để triển khai nhiều instance.

Cấu hình trong Windows PowerShell trước khi khởi động runtime legacy (ADC đã đăng nhập bằng `gcloud auth application-default login`). Chạy từ thư mục `Backend`:

```powershell
$env:GOOGLE_CLOUD_PROJECT = 'trendscout-192e4'
$env:TREND_SCOUT_OPERATOR_TOKEN = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Giữ token trong environment của shell vận hành, tối thiểu 24 ký tự. Nếu thiếu token, POST bị vô hiệu hóa; GET không yêu cầu token. Mặc định server bind localhost; nếu cho phép truy cập qua mạng cần bổ sung TLS, kiểm soát mạng và quản lý token trước. `TREND_SCOUT_RUN_COOLDOWN_SECONDS` mặc định 60; `TREND_SCOUT_LOCK_PATH` mặc định nằm dưới thư mục home của tài khoản. Không xóa file khóa khi server đang hoạt động; mọi process phải cùng một đường dẫn. Nếu worker bị gián đoạn, lần khởi động tiếp theo đánh dấu run dang dở `ABORTED` khi lấy được khóa và kết nối storage.

Ở một cửa sổ PowerShell khác, gửi request bằng token trong environment:

```powershell
$headers = @{ Authorization = "Bearer $env:TREND_SCOUT_OPERATOR_TOKEN" }
$run = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/runs' -Headers $headers
$env:TREND_SCOUT_RUN_ID = $run.run_id
Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$env:TREND_SCOUT_RUN_ID"
Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$env:TREND_SCOUT_RUN_ID/keywords?category=all&sort=score&limit=10&offset=0"
Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$env:TREND_SCOUT_RUN_ID/keywords?q=fashion&category=unclassified"
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

Fixture-based synthetic Fetch là phạm vi kiểm thử tách biệt; `start.ps1` không chọn chế độ này mà chạy profile LIVE đa nguồn. Trong fixture profile, public admission/queue/checkpoint dùng SQLite tạm và legacy run data dùng `FakeStorage`; mọi fixture được gắn `SYNTHETIC`, dữ liệu mất khi dừng. Public Fetch đi qua admission, queue, worker và discovery cho cả bốn source IDs, nhưng handlers chỉ trả fixture (không gọi Wikimedia, SerpApi hay YouTube). Kết quả được gắn `SYNTHETIC`, source delivery `FRESH`, và candidate snapshot là `INSUFFICIENT_EVIDENCE`; không cộng rank với lượt xem hoặc tuyên bố trend thật. Public Fetch trả tối đa 100 candidates từ tối đa 10.000 observations; tìm kiếm/bộ lọc trên dashboard chỉ áp dụng phần kết quả được trả. Không triển khai runtime local ra Internet hoặc coi SQLite local là kiểm soát phân tán production.

### D5: demo-first Fetch và production gate riêng

`DEMO_ACCEPTANCE=100%` là kết quả kiểm thử lịch sử cho dashboard với one-click Fetch, bốn handler fixture cố định gắn nhãn `SYNTHETIC`, keyword discovery/search/filter/detail, UI regression và demo-scope security. Đây không phải profile mà `start.ps1` khởi chạy và không phải production deployment.

`app/sources/live_gateway.py` và `guarded_collector.py` có boundary riêng cho Google/Bing qua SerpApi và YouTube; `D5SourceHandlers` nối ba collector này cùng Wikimedia fixed-date gateway vào fenced worker, synthetic-only cache và `DistributedPublicFetch`. `create_app` chỉ cho phép facade injection trong local/test synthetic mode với feature flag bật tường minh và đủ bốn handler SYNTHETIC; route legacy `POST /api/runs` vẫn yêu cầu Bearer token. `LocalSQLiteAtomicStateStore` là backing file-based thật cho local/test synthetic mode; API integration đi qua shared admission, fenced worker, bốn MockTransport adapters và cache. Offline tests xác minh multi-process admission/quota, idempotency, lease/fencing, cache và tối đa một execution cho một job. `SourceCheckpointJournal` ghi response receipt trước khi settle attempt; crash/recovery tests cho cả bốn source tiếp tục từ receipt đã commit mà không gửi lại provider request. Local quota ledger prune bucket UTC đã qua trong transaction, giữ unknown/malformed/future state để fail closed. Trusted maintenance có thể gọi `PublicAdmission.prune_expired()` kể cả khi Fetch disabled để xóa checkpoint hết hạn và receipt/job metadata theo TTL; lịch chạy production và global shared-cache sweep chưa được cấu hình.

`DistributedWorkerRunner` cung cấp polling loop có graceful shutdown và chuyển lỗi lên process supervisor. Synthetic Firestore multi-host probe kiểm tra atomic job claim; workflow `quota-start`/`quota-reserve`/`quota-inspect` kiểm tra race quota pool qua Google/Bing source IDs; cờ test-only `--crash-first-job` kiểm tra crash sau claim và reclaim bằng worker khác sau khi lease hết hạn. Offline SQLite process test xác minh recovery logic; runbook hướng dẫn supervisor restart và hai-host recovery thực tế. Harness hiện hỗ trợ fingerprint máy Windows/Linux dưới dạng SHA-256, từ chối cả project ID lẫn project number Production trước khi tạo client và từ chối `FIRESTORE_EMULATOR_HOST` để không vòng qua Emulator guard. **Chưa chạy harness trên hai host thực**. `PRODUCTION_MULTI_HOST_ACCEPTANCE=BLOCKED`; cùng máy hoặc SQLite không thay thế shared backing, hai host độc lập và supervisor recovery. `D5_PRODUCTION_READINESS=PARTIAL`, `FOUR_SOURCE_LIVE_VERIFIED=NOT_ACCEPTED`; public live/provider calls tiếp tục disabled. Không dùng Firebase cá nhân hoặc bật production Fetch. Xem [D5 acceptance matrix](../Documents/Design/D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md), [kiến trúc D5](../Documents/Design/D5_LIVE_MULTI_SOURCE_SYSTEM_ARCHITECTURE.md) và [multi-host runbook](../Documents/Design/D5_MULTI_HOST_TEST_AND_DEPLOYMENT_RUNBOOK.md).

Website không có luồng đăng nhập, đăng xuất, đăng ký hoặc tài khoản người dùng. `GET /api/v1/capabilities` đọc công khai trạng thái runtime và bốn mô tả nguồn; response không chứa credential. `POST /api/v1/fetches` là endpoint riêng, không thay đổi `POST /api/runs` (endpoint cũ vẫn yêu cầu Bearer operator token). Request public chỉ nhận JSON `{}`; backend chọn plan, nguồn và ngân sách; client không thể truyền URL/query/provider/budget. Ngoài runtime local legacy, `create_app` có thể nhận injected `DistributedPublicFetch` chỉ trong local/test synthetic mode, với policy bật tường minh, đủ bốn handler khai báo SYNTHETIC và `AtomicStateStore` do harness cung cấp. API worker path không cần Firebase/FakeStorage, nhưng không tự tạo backing và không được bật trên production/Vercel.

Public Fetch mặc định tắt qua `TREND_SCOUT_PUBLIC_FETCH_ENABLED=false`. Legacy runtime cần `FakeStorage` và adapter fixture. Worker facade dùng atomic admission/idempotency/quota/poll budgets và handler cố định, nhưng chỉ được inject trong profile `local`/`test`, mode `synthetic`, không read-only, không project cloud và flag bật rõ ràng. `LocalSQLiteAtomicStateStore` từ chối production/live/memory DB, giới hạn transaction trên một host/local filesystem, và không chứng minh backing đa host. Cả hai đường đều giữ body tối đa 1 KiB và không nhận query/provider/budget từ client.

Các giới hạn này chỉ xác nhận luồng synthetic trong môi trường local/test cô lập. Chúng **không** bảo vệ giữa nhiều host/container độc lập, không chống bot phân tán qua quota bền vững và không đủ để mở public live Fetch. Origin/Host/Fetch Metadata là tín hiệu chống lạm dụng trình duyệt, không xác thực client; client HTTP trực tiếp vẫn gọi được endpoint nếu deployment bật. Vì vậy production/Vercel, Wikimedia live, YouTube, Google Search, Bing Search và mọi provider tính phí đều bị khóa. Không có yêu cầu hoặc thao tác Firestore trong D3A.

Chạy bộ kiểm chứng Windows bằng `.\verify_offline_readiness.ps1` từ repository root. Các test fixture dùng adapter giả lập và fake storage; chúng không gọi provider thật, Firestore hoặc emulator. Public live chỉ được xem xét sau khi có phê duyệt riêng và xác minh distributed rate/concurrency/idempotency, durable quota/cost budgets, bot/abuse controls, monitoring và provider approvals.

### D6.1B: guarded local LIVE HTTP runtime (offline implementation)

`app.main.create_local_live_app` exposes the existing bounded D6 facade through `GET /api/v1/capabilities`, `POST /api/v1/fetches`, and `GET /api/v1/fetches/{fetch_id}`. It requires `enabled=True`, a caller-supplied async composition factory, local environment settings, live run mode, and one worker. It wraps the existing FastAPI application with `LocalLiveGuard`; only loopback HTTP peers, local Host values, same-origin requests, and those API routes are accepted. Fetch JSON remains `{}` and the server creates the internal fetch ID. Replayed idempotency keys return the actual job status without scheduling another terminal job.

Construct it in an operator-owned Python entry point after explicitly supplying credential, approval, checkpoint-retention, and processing-grant providers to `local_live_composition`. The app factory does not read `.env`, discover credentials, or grant provider permissions. Bind only `127.0.0.1`, use one Uvicorn worker, and do not expose the port through a tunnel or reverse proxy. For example, after defining an explicit `compose_for_this_machine()` context factory:

```python
from app.main import create_local_live_app, run_local_live

app = create_local_live_app(
    enabled=True,
    workers=1,
    composition_factory=compose_for_this_machine,
)
run_local_live(app, port=8001)
```

App construction and startup do not run a Fetch. A user POST can trigger the existing source handlers, and only a backend-owned source approval can make a source available. A missing processing grant yields no derived candidates. `LocalLiveGuard` is not authentication against other local processes. The composition still uses the one-job/attempt limits and shared SerpApi pool; do not delete or reset its SQLite ledger to regain allowance. Source checkpoints are volatile and logically TTL-bounded, but physical erasure from SQLite WAL, swap, crash dumps, backups, or logs is not guaranteed. Vercel and the normal `create_app()` path remain outside this live composition. The runtime is not provider-live verified; all D6.1B validation must use fixture handlers or mock transports.

## D3B — YouTube search adapter: giai đoạn offline ban đầu

Phần này ghi lại ranh giới của D3B. D6 sau đó bổ sung đường LIVE local có guard, quota, lịch, cache và Source Center; xem mục **D6 — YouTube Data API v3** bên dưới. D3B contract tests dùng MockTransport; đường LIVE D6 đi qua `LivePageGateway` và không dùng adapter fixture như bằng chứng LIVE.

`app/sources/youtube/` triển khai adapter query-based riêng cho YouTube Data API v3 `search.list`; không ép video search resource vào `PageObservation`/Wikimedia scoring. Adapter dùng fixed endpoint và allowlist tham số từ plan server-side `demo-us-en` v1, deduplicate theo provider/video ID nhưng giữ từng query evidence, và chỉ tạo descriptor `result_rank` (vị trí trong query response), không tạo views, trend score hay demand score.

Boundary của adapter D3B đọc credential server-side theo tên `YOUTUBE_API_KEY` và gửi qua header `X-Goog-Api-Key`, không đưa key vào URL. Dữ liệu trong contract fixtures là `SYNTHETIC`; quota tracker của adapter không suy ra quota còn lại của Google Cloud project.

Query plan đăng ký vẫn có 14 seed, nhưng D6 live planner chỉ chọn tối đa hai scope curated cho mỗi slot đủ điều kiện. Cursor được gắn với query/plan/filter/cutoff context; page cap, request budget, retry wait, deadline, cancellation, malformed body, quota và rate-limit có stop/error state riêng. Request/response body không được lưu. D6 chỉ dùng raw video evidence; keyword extraction và derived metrics YouTube tiếp tục bị tắt khi chưa được duyệt.

Chạy adapter contract tests trong PowerShell từ repository root:

```powershell
.\Backend\.venv\Scripts\python.exe -m unittest discover -s Backend\tests -p 'test_youtube_adapter.py' -v
```

Các kiểm tra tích hợp/regression nằm trong `.\verify_offline_readiness.ps1` từ repository root. Tham khảo [báo cáo D3B](../Documents/Reports/BAO_CAO_DEMO_D3B.md) và [source capability matrix](../Documents/Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md). Tài liệu Google được đối chiếu ngày 11/11/2026; quota của project thật và quyền dùng/lưu dữ liệu chưa được xác minh hoặc phê duyệt.

### Ngân sách kiểm thử Bước 10

Trước khi ghi bucket, backend ước lượng số document writes (snapshot + observation chunks + 16 shard kết quả + 10 lượt dự phòng trạng thái) và dừng nếu vượt 1.000. Đây là guard số thao tác dự kiến, không phải đồng hồ quota Firestore chính xác; retry, ghi từ process khác và độ trễ hiển thị usage có thể ảnh hưởng con số thực tế. Các test có thể truyền `run_mode="synthetic"` hoặc `"replay"` vào `create_app` khi inject nguồn giả, để dữ liệu test không được gắn nhãn live. Production mặc định live và không cho client chọn mode.

## Historical Vercel deployment snapshot (2026-11-17; superseded)

This section records the pre-D6 deployment state. The writable Shared LIVE design above supersedes it for current Vercel behavior.

**Trạng thái code:** hotfix mới chỉ được kiểm tra offline; chưa xác minh deployment Vercel mới. Deployment `dpl_EZbtefvf2PqprB7bgrUtnJTStTMH` đã được kiểm tra ở trạng thái READY nhưng runtime trả HTTP 500 cho `/`, `/dashboard/` và `/api/health`; runtime log báo thiếu `fastapi`. Build log chọn `pyproject.toml`, trước hotfix file này không khai báo runtime dependencies, còn `requirements.txt` không được cài. Bản sửa khai báo toàn bộ runtime package đang pin trong `[project].dependencies`; `requirements.txt` chuyển tiếp về project metadata để chỉ có một danh sách pin. `requires-python` giới hạn deployment ở Python 3.12. Tài liệu Vercel xác nhận Python runtime đọc dependencies và khoảng phiên bản interpreter từ các trường này ([Python runtime](https://vercel.com/docs/functions/runtimes/python), [FastAPI trên Vercel](https://vercel.com/docs/frameworks/backend/fastapi); đã kiểm tra 11/11/2026).

Windows `start.ps1` khởi chạy profile LIVE đa nguồn local. Trên Vercel (`VERCEL=1`), ứng dụng vẫn chỉ đọc: không tạo `RunService`/`HostRunLock`, không recovery run và không chạy pipeline. GET `/` redirect sang `/dashboard/`; HTML, CSS, JavaScript modules, `/api/health` và `/api/v1/capabilities` công khai ở tầng ứng dụng. GET `/api/runs/{run_id}` và các route kết quả vẫn riêng tư, cần HTTP Basic (`viewer` cùng `TREND_SCOUT_READ_PASSWORD` dài ít nhất 32 ký tự). Nếu thiếu secret này, đọc run bị chặn với 503; dashboard công khai không phụ thuộc vào nó. Đọc run thành công còn cần Vercel OIDC/Firestore identity được cấu hình. Ứng dụng không mở rộng quyền Firestore. POST `/api/runs` và POST `/api/v1/fetches` trả 405. Không đưa operator token vào demo online.

Vercel API metadata được đọc chỉ đọc ngày 17/11/2026 báo SSO protection đang bật với `all_except_custom_domains`. Metadata liệt kê `GOOGLE_CLOUD_PROJECT` và `GOOGLE_WIF_AUDIENCE` cho Production, nhưng cả hai có `visibility=secret` và được trả về với `decrypted=false`, nên giá trị chưa được đối chiếu. Project metadata không trả về trạng thái Secure Backend Access/OIDC; SSO không chứng minh OIDC đã bật. Không thực hiện token exchange, đọc document production, kiểm tra route live hoặc deploy trong lần kiểm tra này. Giữ ứng dụng Vercel ở read-only cho đến khi các gate còn lại PASS.

1. Historical note: the previous project configuration used Root Directory `Backend`, Framework Preset FastAPI, and **Include source files outside of the Root Directory in the Build Step**. Current builds stage non-hidden `Frontend/public` assets under `Backend/public/dashboard`; Vercel serves the FastAPI `StaticFiles` mount from its CDN, while API routes remain in the Python function. Vercel installs the core dependencies from `Backend/pyproject.toml`; local setup through `requirements.txt` adds the `local-firestore` extra for the Firebase Admin adapter used by the explicit local/legacy Firestore mode. Python is constrained to 3.12.
2. Khi cần private run reads, bật Secure Backend Access/OIDC cho Vercel và chọn team issuer. Cấu hình Google Workload Identity Provider với issuer `https://oidc.vercel.com/rakoon1`, source-token audience `https://vercel.com/rakoon1`, và subject chính xác `owner:rakoon1:project:trend-scout:environment:production`; giới hạn điều kiện provider cho project/environment này. `GOOGLE_WIF_AUDIENCE` là resource URI của provider, khác với `aud` trong Vercel token. Mã hiện tại pin resource URI `//iam.googleapis.com/projects/556422620769/locations/global/workloadIdentityPools/trendscout-vercel/providers/vercel-production` và từ chối URI khác. Google STS vẫn phải xác minh chữ ký token và điều kiện provider. Tham khảo [Vercel GCP federation](https://vercel.com/docs/oidc/gcp) và [Vercel OIDC claims](https://vercel.com/docs/oidc/reference).
3. Runtime dùng Google Auth external-account credentials lấy token từ request header rồi impersonate service account `trendscout-vercel-backend@trendscout-192e4.iam.gserviceaccount.com`; không dùng key JSON hoặc ADC fallback. Workload identity principal phải có `roles/iam.workloadIdentityUser` trên đúng service account và bị giới hạn cho subject production. Firestore role phải gắn cho service account và có điều kiện database `(default)`. Chủ sở hữu báo đã gán `roles/datastore.user`; role này cấp quyền đọc/ghi dữ liệu, nên Vercel application vẫn giữ read-only HTTP routes và Shared LIVE chưa được nối vào production. Xác minh quyền IAM thực tế trước khi dùng. [Google WIF impersonation guidance](https://docs.cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation), [Firestore IAM](https://docs.cloud.google.com/firestore/native/docs/security/iam).
4. Khi cần private run reads, cấu hình `GOOGLE_CLOUD_PROJECT=trendscout-192e4`, `GOOGLE_WIF_AUDIENCE` bằng provider resource URI ở trên và `TREND_SCOUT_READ_PASSWORD` trong Production. Không cần password để render dashboard hoặc đọc capability. `VERCEL` và OIDC request header do Vercel cung cấp; không tự điền hoặc lưu token. Không dùng ADC hoặc service-account key. Tránh cấp WIF cho preview.
5. Sau deploy mới: trước hết kiểm tra Deployment Protection/domain riêng, rồi xác nhận `/` trả redirect, dashboard HTML/CSS/JS và capability tải được không cần app password, còn run GET bị chặn khi không có app password và POST `/api/runs` + POST `/api/v1/fetches` trả 405. Nếu private reads được cấu hình, kiểm tra bằng một run ID đã biết và xác nhận OIDC/IAM chỉ cho phép đọc. 503 cho thấy private read chưa cấu hình hoặc Firestore không truy cập được. Không coi local tests/build log là xác minh production; đọc logs mà không chia sẻ token. Đọc qua Vercel có thể tiêu tốn Firestore reads.

[Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi) · [OIDC trên mọi plan](https://vercel.com/docs/oidc) · [Chia sẻ source ngoài root](https://vercel.com/docs/monorepos/monorepo-faq). Tài liệu OIDC và WIF đã được kiểm tra ngày 17/11/2026.

## Local Google Trends Fetch (D6)

Windows `start.ps1` launches the normal multi-source LIVE composition described here; it does not fetch at startup. The dashboard Fetch control is the user action that starts source collection. An empty quota ledger is accepted on first launch and initialized when Fetch is first admitted. A populated ledger is preserved and must match one of the supported explicit migrations.

The Windows profile enables Google Trends Trending Now, Google SERP and Bing Search through SerpApi, plus Wikimedia Pageviews and YouTube Search. The free SerpApi Account API preflight runs before paid provider work; Google and Bing share its local quota ledger. Bing remains capped at one candidate and one page per eligible user Fetch. Google RSS remains disabled. Python reads `SERPAPI_API_KEY` directly from `.env` at the repository root; `Backend/.env` is not read.

Create the local file from the non-secret template and enter the recipient's own SerpApi key:

```text
SERPAPI_API_KEY=your_serpapi_key
```

Quoted values and a UTF-8 BOM are accepted; shell expansion and inline comments are not used. On Windows, the `.env` file DACL must allow only the current owner, SYSTEM and local Administrators; a typical command from PowerShell is `icacls .env /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):F" "*S-1-5-18:F" "*S-1-5-32-544:F"`. The old `%LOCALAPPDATA%\TrendScout\.env` location remains a fallback for existing Windows installs. `.env` is ignored by Git; share it only through the credential handoff channel you use for that recipient, never commit or paste the key into frontend configuration.

Paid Google and Bing refreshes use the server-persisted, user-triggered schedule **07:00, 15:00 and 23:00 Asia/Ho_Chi_Minh**. Each source attempt must be at least eight hours apart, and a missed slot is not replayed. A fresh local cache is delivered without a provider request. A stale or missing cache can refresh only after a manual Fetch in an eligible slot. On a host with no history for a source, one explicit first-run acceptance exception allows that source to be verified by a manual Fetch; subsequent attempts follow the regular slots. Newly admitted Google Trends and Bing Search requests use SerpApi `no_cache=true` so provider-side cached results are not mistaken for newly fetched data. Startup and background processing do not fetch provider data.

The Google LIVE run uses one bounded HTTPS request with `Accept-Encoding: identity`, strict response-size and timeout limits, and no provider retry. It checks the SerpApi Account API before an admitted paid request. HTTP transport failures retain only sanitized phase, status, exception class and byte-count diagnostics; request URLs, headers, response bodies and credentials are excluded.

The current local quota policy is shown by the live Account API and Source Center; do not infer provider remaining quota from a local `.env` file. Local SQLite reservations do not coordinate separate computers, so the local schedule and reserve cannot prevent a simultaneous request from another host.

The lower-level `GoogleLiveSettings` environment switches still apply to directly composed runtimes and tests; they are not launcher arguments. They do not prove provider entitlement or permission. Do not enable Google SERP, RSS or Bing by changing browser state.

## D6 — YouTube Data API v3

D6 adds the official YouTube `search.list` source to the normal local LIVE Fetch pipeline, with bounded `videos.list` support. Windows `start.ps1` designates the current machine for YouTube LIVE when its key is configured; it does not trigger Fetch at startup. The dashboard user selects an available source and clicks Fetch; schedule, cache, admission and quota checks remain server-owned.

Put `YOUTUBE_API_KEY` in the private project-root `.env` file described above. The credential provider reads that file; the Windows launcher supplies process-scoped LIVE approval and designates only the current machine, then restores the shell's prior environment values when it exits. The owner-approved Privacy Policy and Terms are published, and their approval is versioned in source code. The dashboard requires an explicit user agreement to the current policy version before YouTube Fetch. Project/API-key setup does not itself require a published Privacy Policy; YouTube Developer Policies require it before users access API-backed features. A key being present does not prove the API is enabled or that the project has the documented default quota.

The selected host value must match `socket.gethostname()` on the one machine allowed to send provider requests. Do not copy that host designation to a second machine: local SQLite ledgers do not coordinate quota across hosts. Other hosts may only read their own persisted cache. Current acceptance status and exact owner-side blockers are recorded in [D6 YouTube acceptance](../Documents/Reports/D6_YOUTUBE_LIVE_ACCEPTANCE.md) and [the policy review](../Documents/Reports/evidence/youtube-audit/POLICY_REVIEW_AND_OWNER_DECISIONS.md).

Internal ceilings are six `search.list` calls per America/Los_Angeles provider day and 200 per UTC calendar month; the separate `videos.list` pool is six units per provider day and 200 per UTC month. These are application limits, not the Google Cloud project's quota. Provider quota remains `UNKNOWN` until Cloud Console evidence is reviewed. The default `NORMAL_DEMO` runtime is manual-only at 07:00, 15:00 and 23:00 Asia/Ho_Chi_Minh, with a ten-minute slot grace and at least eight hours between attempts. Fresh cache is delivered without a provider request; expired YouTube cache entries are removed by the focused cleanup path. YouTube records remain source evidence and do not enter keyword extraction, collection scoring or composite trend metrics.
