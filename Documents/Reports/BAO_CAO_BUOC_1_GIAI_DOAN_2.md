# TrendScout Giai đoạn 2 — Báo cáo Bước 1: Audit và baseline

**Ngày:** 09/11/2026. **Phạm vi:** audit source/tài liệu/Git và kiểm thử offline; không refactor, gọi Wikimedia, ghi/xóa Firestore, chạy AI, thay IAM, deploy hay bật billing. Báo cáo này phản ánh quan sát tại thời điểm audit, không chứng minh điều kiện cloud hiện tại.

## 1. Baseline và phạm vi Git

- Baseline code và tài liệu khi bắt đầu: `f54339943a81cfc59563751303ea247420a91ccd` (`docs: add TrendScout phase 2 implementation guide`), trên `main`, trùng `origin/main` khi đối chiếu remote. Không có commit cũ chờ push hoặc thay đổi chưa commit.
- Commit hygiene thực hiện trước báo cáo: `ae9b24c` (`chore(repo): clean tracked files and update gitignore`); thay đổi ignore, không thay code hoặc dữ liệu live. Do đó baseline hành vi code vẫn là `f543399`.
- Remote: `https://github.com/trandaiquoc/TrendScout.git`. Báo cáo commit riêng sau commit hygiene; trạng thái push cuối cùng cần xem `git status -sb` và báo cáo bàn giao của phiên làm việc, không giả định chỉ từ nội dung file này.
- Đã kiểm tra danh sách file tracked, `git ls-files --cached --ignored --exclude-standard`, đường dẫn ignored và lịch sử tên file. Không có file tracked khớp ignore, không tìm thấy đường dẫn lịch sử khớp mẫu env, private key, credentials, cache hoặc build đã kiểm tra; không cần `git rm --cached`. Không xóa file local.
- `.env`, `.venv`, `__pycache__` hiện bị ignore. `.gitignore` bổ sung `.idea/`, các file VS Code cá nhân, `*.code-workspace`, `.DS_Store`, swap, `node_modules/`, `.vercel/`, `.secrets/`, private-key extensions và các đường dẫn dữ liệu local/cache/snapshot/database **cụ thể** dưới Backend. Các tên credential có thể tùy ý: ignore không bảo đảm ngăn mọi secret.
- Đã thử 14 đường dẫn cần ignore và 14 đường dẫn cần giữ, bao gồm `AGENTS.md`, Documents, README, source, tests, fixture, manifests, `.python-version`, `vercel.json`, `setup.sh`, `start.sh`, build script và `.env.example`; kết quả đúng. `Backend/app/dashboard_assets.py` là build artifact đã ignore, còn `Backend/scripts/prepare_vercel.py` và source frontend được theo dõi vì cần build.
- Quét 91 text blobs trong lịch sử reachable và nội dung XML của 1 DOCX bằng các chữ ký private key, Google API key, GitHub token, OpenAI key/service-account private key: **không có match**. Không in nội dung secret. Giới hạn: quét chữ ký không phát hiện được mọi định dạng, token tùy ý, binary khác hoặc credentials từng được thu hồi/xóa ở refs không còn reachable; không chứng minh tuyệt đối không có secret. Rà soát thêm assignment giống credential trong lịch sử thấy `Backend/README.md` và `Frontend/README.md` chỉ là ví dụ shell substitution để sinh token local, không phải giá trị token được lưu. Không có bằng chứng đủ để kết luận `SECRET_EXPOSURE_SUSPECTED`; nếu kiểm tra tiếp phát hiện secret thật phải rotate/revoke, báo đường dẫn/loại, tạm dừng push có rủi ro, không viết lại history/force push tự ý.

## 2. Tổng quan kiến trúc thực tế

```text
Browser: Frontend/public (HTML/CSS/JS)
        ↓ cùng origin
FastAPI app/main.py → api/health.py, api/runs.py
        ↓ create/start + asyncio task (local)
RunService → WikimediaAdapter.collect() → clean_snapshot()
           → extract_keywords() → score_trends() → AnalysisStorage
                                      ↓                   ↓
                            version/quality/evidence   FirestoreStorage / FakeStorage
```

| Thành phần | Hiện có, khả năng tái sử dụng | Giới hạn/coupling cần bảo toàn và đánh giá |
|---|---|---|
| Backend | `app/main.py` có `create_app`, dependency truyền qua tham số, lifespan, health/router, middleware, dashboard. | Composition, local lifecycle và read-only nằm chung; `RunService` do app khởi tạo; không coi health là kiểm tra Firestore. |
| Nguồn | `adapters/base.py` có `SourceAdapter.fetch_day`; `wikimedia.py` dùng HTTPX, validate response/ngày, 404/429, retry, User-Agent và provenance. | `RunService` phụ thuộc trực tiếp `WikimediaAdapter.collect`, interface `fetch_day` chưa mô tả toàn bộ collect; source/scope cụ thể chảy vào scoring. |
| Models/validation | Dataclass `DatasetSnapshot`, `PageObservation`, `KeywordResult`, `TrendResult`, enum trạng thái; adapter và cleaning có validate. | Dataclass chứa trường đặc thù Wikimedia; chưa có canonical metric/unit chung cho nhiều nguồn; validation phân tán. |
| Cleaning | `clean_snapshot`: normalize, rule technical pages, dedup theo scope/ngày/project/article, báo rejected/duplicate và quality. | Rule thuộc Wikimedia, cần giữ riêng; báo cáo demo còn ba title dạng `*:Main Page` có vẻ kỹ thuật chưa xử lý, không âm thầm coi đã sửa. |
| Keyword/classification | Giữ title, original, project/source/scope/day, evidence; `title-rules-v0.1`, category `fashion`, `home_goods`, `other`, `unclassified`. | Phụ thuộc title/rule; một phần tên người chưa đủ context, không có taxonomy đa cấp/AI/đánh giá ground truth. |
| Scoring | `analysis/trend.py` có hàm độc lập và `DayData`, version, lý do thiếu, evidence; phù hợp golden test. | Import hằng `SOURCE` của adapter và giả định `views_ceil`; chưa có strategy/registry hoặc evaluation bộ nhãn. |
| Storage | `AnalysisStorage` protocol, `FirestoreStorage` + `FakeStorage`; mapping và lỗi đã có unit test. | Record là mapping linh hoạt, chấp nhận cả `baseline_buckets` nhưng pipeline hiện ghi baseline trong keyword result; chưa có storage contract nghiệp vụ versioned. |
| Điều phối | `HostRunLock` dùng `fcntl.flock`, cooldown, marker, recovery và asyncio background task; ngân sách 1.000 writes, chunk 40, 16 shards. | Khóa chỉ cùng host/filesystem; không scheduler, durable queue, lease/fencing hay multi-host. Startup local có thể recovery và ghi trạng thái, không dùng để thử cloud trong audit. |
| Dashboard | HTML/CSS/JS cùng origin; nhập run ID, tìm/ lọc/ phân trang/chi tiết, fetch thủ công; local đọc token operator trong dialog. | Không có API liệt kê runs, không có account/tenant, settings AI, scheduler hay quyền sản phẩm; Basic auth demo không thay thế RBAC. |
| Local | `setup.sh` tạo venv/env; `start.sh` dùng Python trong `.venv`, localhost:8000, ADC và không tự tạo run. | Lệnh khởi động dùng storage theo môi trường, không khẳng định cloud healthy; giữ entrypoint và quyền Ctrl+C khi refactor. |
| Vercel | `VERCEL=1` ép read-only; không khởi tạo `RunService`, lock hay startup recovery; HTTP Basic bảo vệ dashboard/run reads, POST bị chặn; build script đóng gói frontend. | OIDC request token → google-auth WIF → Firestore client là code + fake/mock; chưa có bằng chứng Vercel build/deploy hoặc WIF/IAM read-only live. Thiếu password/token/provider cấu hình trả lỗi; không có rate limit đăng nhập ứng dụng. |

### 2.1. Boundaries ưu tiên cho refactor sau này

Đề xuất dựa trên coupling thực tế, **không thực hiện trong bước này**: contract source `collect` + coverage/capability; repository versioned cho dataset/run và fake contract; interface orchestration/clock/lock/worker; cleaning stages source-specific; thuật toán strategy registry trả kết quả/skip/evidence; composition root/DI cho adapters và configuration. Tách use case khỏi FastAPI và Firebase SDK; không tạo lớp cho pattern nếu không giải quyết thay đổi thực tế. Migration nhỏ, adapter compatibility, golden trước khi di chuyển; giữ `fcntl` local và chỉ thay khi distributed lease được kiểm thử riêng.

## 3. API hiện hữu và hợp đồng

| Method/path | Request | Response/hành vi hiện tại |
|---|---|---|
| `GET /api/health` | không cần body | `200 {"status":"ok"}`; không kiểm tra provider/storage. |
| `GET /api/config` | không cần body | `{ "read_only": bool }`; được dashboard dùng ẩn nút fetch. |
| `POST /api/runs` | local: `Authorization: Bearer <operator token>` | `202 {run_id,status:"QUEUED"}`; 401 sai auth, 503 thiếu token/storage, 409 busy, 429 cooldown. Read-only trả 405 và không khởi tạo pipeline. |
| `GET /api/runs/{run_id}` | 32 ký tự hex thường | metadata/status hoặc 404, 503 khi storage lỗi/chưa cấu hình. |
| `GET /api/runs/{run_id}/keywords` | `q` tối đa 100, `category=all|fashion|home_goods|unclassified`, `sort=score|views|keyword`, `limit` 1–100 mặc định 50, `offset` 0–10000 | `{items,total,offset,limit,next_offset,empty_reason}`; 409 nếu chưa có kết quả, 503 nếu shards không đầy đủ. Search trong dữ liệu đã lưu, không gọi nguồn. |
| `GET /api/runs/{run_id}/keywords/{keyword_id}` | run ID và keyword ID hex dài 64 | result chi tiết có baseline/evidence; 404 không có, 409 chưa sẵn sàng, 503 shard thiếu. |
| `GET /dashboard/`, JS/CSS | cùng origin | local static; read-only được bảo vệ bằng Basic và `Cache-Control: no-store`. |

Run status: `QUEUED → FETCHING → NORMALIZING → ANALYZING → PERSISTING → COMPLETED/PARTIAL`; lỗi `FAILED` hoặc `PARTIAL` tùy dữ liệu đã ghi, shutdown/recovery có thể `ABORTED`. Không giả định mọi đường lỗi đều đạt terminal nếu Firestore lỗi khi update; marker phục vụ recovery. Mode `live`, `synthetic`, `replay` phân biệt nguồn kết quả. Snapshot `SUCCESS`, `PARTIAL`, `DAY_UNAVAILABLE`, `FETCH_ERROR`; trạng thái observation `OBSERVED`, `NOT_IN_TOP`, `DAY_UNAVAILABLE`, `FETCH_ERROR`.

Xác thực: local POST cần operator token qua env, GET local không có xác thực riêng; Vercel read-only dùng Basic password và username `viewer` cho dashboard/run, `/api/health` công khai. WIF nhận OIDC token request qua `x-vercel-oidc-token`, provider audience và project từ env; không fallback ADC ở code read-only. Server Firebase Admin/Google Cloud client dùng IAM, không dựa vào Firestore client Security Rules. Không có xác minh IAM/WIF live ở bước audit.

## 4. Dữ liệu, Firestore và provenance

Nguồn `wikimedia_pageviews_top_per_country`, US/all-access, ngày UTC theo endpoint; cả scope/project gắn từng observation. Ngày phân tích gần nhất khả dụng và 7 ngày lịch trước; page vắng trong top không suy ra 0. `views_ceil` là xấp xỉ/cận trên, không là purchase intent hay số người dùng. Không suy các chủ đề ngoài endpoint hoặc mọi social trend từ mẫu top.

```text
analysis_runs/{run_id}                         metadata/state, source, mode,
                                               created/started/finished/fetched,
                                               data_date, request_count, coverage,
                                               observation/keyword/scored counts,
                                               document_writes, versions, error
  snapshots/{YYYY-MM-DD}                        fetch status/http/count/error,
                                               raw rejections + cleaning report
  observations/{YYYY-MM-DD}-{chunk:04d}        date + items (tối đa 40/run chunk)
  keyword_results/shard-{00..15}               items theo chữ số hex đầu keyword_id
  baseline_buckets/{bucket_id}                 được interface cho phép, pipeline
                                               demo hiện không ghi collection này
```

Mỗi keyword result hợp nhất keyword và trend record: `keyword_id`, `observation_id`, `source`, `country_scope`, `access`, `project`, `article_original`, `canonical_keyword`, `display_keyword`, `search_keyword`, `categories`, `classification_version`, `classification_evidence`, `observation_date`, `fetched_at`, `views_ceil`, `rank`, `source_url`, `raw_record`, `quality_flags`; scoring thêm `current_views_ceil`, `baseline`, `ratio`, `score`, `status`, `observed_baseline_days`, `baseline_observations`, `missing_baseline_dates`, `score_components`, `algorithm_version`, `evidence_url`. Metadata snapshot có `request_url`, `http_status`, `request_count` và report cleaning. Keys mang bản sắc source/project/article; observation còn scope/date. Không gộp project khác/ngày khác. `null`, missing, `0`, nguồn 404, lỗi, not-in-top và rejected khác nhau.

Không có API liệt kê run hoặc xóa dataset; Firestore xóa parent không tự xóa subcollections. Batch viết từng document không tạo transaction toàn run, status `COMPLETED` chỉ sau các content writes, nhưng có thể phát sinh partial/recovery khi lỗi. Mỗi document chịu guard 700.000 bytes JSON để chừa khoảng cho giới hạn nhà cung cấp; 16 shards là chi tiết lưu trữ hiện hữu, không phải API đảm bảo mãi mãi. Backend README có phần mô tả Bước 3 cũ rằng chưa có API run; phần đó lỗi thời, audit dựa trên source/router và các phần hướng dẫn mới hơn.

## 5. Thuật toán trend v0.1 và bất biến

Version `wikimedia-trend-v0.1`: với entity hiện diện ngày phân tích, current = `views_ceil`; baseline = median của tối đa 7 ngày lịch trước mà cùng source/scope/project/article có observation hợp lệ. Tối thiểu 3 ngày quan sát; missing không là 0. `max_views_ceil` là max current hợp lệ của toàn tập candidates trong cùng source/country/access ngày phân tích, kể cả entity thiếu baseline.

```text
G = clip(log2(current / baseline) / 3, 0, 1)
V = log1p(current) / log1p(max_views_ceil)
score = round(100 * (0.7 * G + 0.3 * V))
```

Chỉ tính khi current, baseline, max dương hợp lệ; ngược lại `score=null`, `INSUFFICIENT_DATA`. `EMERGING` nếu ratio ≥ 1,5 và score ≥ 50; `FALLING` nếu ratio < 0,75; còn lại `STABLE`. Ghi `CENSORED_TOP_LIST`, `VIEWS_APPROXIMATE`, incomplete coverage và missing reasons khi tương ứng. Baseline trên top có selection bias; đây là heuristic, không là xác suất hay đo nhu cầu mua.

**Không được phá khi refactor:** source identity/scope/date; raw/article/project; date/window và rank/measurement; khả năng đối soát cleaning; semantic `0/null/missing/censored`; formula/rounding/max và version/status; evidence/quality flags; idempotency ID và đường dẫn Firestore cũ; API/filter/pagination; báo `PARTIAL`/lỗi trung thực; localhost/start.sh và Vercel fail-closed; không bật nguồn, AI, schedule hoặc ghi ngoài ý muốn.

## 6. Đối chiếu T1–T5

| Nhóm | Hiện trạng | Khoảng cách/phụ thuộc và rủi ro |
|---|---|---|
| T1 | Có module adapter, protocol storage, dataclass, DI tham số và các function tách lớp. | Orchestrator biết source/scoring, adapter protocol chưa bao `collect`, framework routes biết storage; chưa registry/strategy đầy đủ. Trước di chuyển cần golden API/schema và baseline tests. |
| T2 | Wikimedia validation, cleaning/dedup, title classifier, một scorer. | Chưa multi-source cleaning, language detection, AI translation, near-dedup, engine/evaluation nhãn. Nguy cơ gộp nhầm entity, mất provenance và bịa score; tách data contract và thử applicability. |
| T3 | Firestore shards/buckets, write budget 1.000, lock local và lịch sử 7 ngày trong từng run. | Không có shared dataset, scheduler, retention, distributed locking hoặc cleanup; quota/giá thay đổi, xóa subcollection/atomic active là rủi ro. Refresh và retention cần policy độc lập và user duyệt. |
| T4 | Title rules đơn giản, `unclassified` và evidence. | Không có taxonomy tổng thể, AI provider, secret store, research/market evidence; tránh nhầm attention với sức mua, hallucination, IP và quyền nguồn. |
| T5 | Dashboard demo và operator token; Vercel Basic read-only code. | Thiếu auth product, tenant isolation, RBAC, job controls, AI settings, abuse protection; BYOK/keys chỉ server, không xây theo Basic demo. |

Bước kế tiếp là golden regression & contract API theo hướng dẫn Giai đoạn 2; chỉ sau đó thực hiện từng refactor nhỏ. Các bước T1–T5 chưa được triển khai trong lần audit.

## 7. Kiểm thử baseline thực tế

| Kiểm tra offline | Kết quả |
|---|---|
| `cd Backend && .venv/bin/python -m unittest discover -s tests -v` | **49 pass, 0 fail, 0 skip**. Bao gồm fake/integration, Vercel read-only, mock STS, adapter/cleaning/scoring/API. |
| `cd Backend && .venv/bin/python -m pip check` | `No broken requirements found.` |
| `bash -n start.sh setup.sh` | Thành công. |
| `node --check Frontend/public/app.js` | Thành công; Node có sẵn. |
| Git ignore paths và `git diff --check` | Thành công; không có tracked ignored hiện hữu. |

Test offline không chứng minh live Firestore, Wikimedia hiện tại, Vercel, IAM, quota, Browser E2E hay nhiều host. Báo cáo demo ghi **một** live run đã đọc lại sau restart, 7.737 observations, 968 keywords, 226 content writes, khoảng 232 writes gồm trạng thái; đó là bằng chứng lịch sử có ngày, không phải phép đo mới. Báo cáo demo còn ghi 3 trang kỹ thuật có thể lọt cleaning, fashion/home rỗng và Cloud Monitoring `403`; không suy sai thành quota hết. Không fetch/ghi/xóa cloud ở bước này.

## 8. Golden fixture đề xuất và điều kiện chuyển Bước 2

- Dùng fixture tổng hợp hiện có trong tests hoặc tạo fixture synthetic nhỏ, có version, policy và kỳ vọng tính tay; không xuất raw live hoặc thông tin nhạy cảm nếu chưa có quyền lưu/chia sẻ. Fixture synthetic **không phải live**.
- Coverage tối thiểu: nhiều project/title và ngày; technical namespace/title bình thường; accepted/rejected/duplicate đối soát; `views=0`, missing/null, 404/429/timeout, `NOT_IN_TOP`, partial/rejected; danh mục không rõ và rỗng; exact score, current max khác biệt scope, baseline 2/3/7 ngày; sort/filter/pagination/empty/errors; persisted old-shard mapping; read-only access và API health.
- Golden cần tách các field không deterministic như timestamp/random ID: cố định clock/ID bằng injection hoặc xác thực theo schema; không xóa field quan trọng khỏi assertion. Lưu expected version và provenance. So sánh trước/sau, không chấp nhận thay đổi công thức/API chỉ để test pass.
- Trước Bước 2 xác nhận quyền dữ liệu fixture, version baseline và scope; làm rõ mọi user change mới và run toàn test. Không cần AI key, Firebase write, billing hay live fetch cho golden. Nếu chọn fixture live phải kiểm tra license/retention, xóa thông tin không được phép và được user phê duyệt riêng.

## 9. Quyết định nghiệp vụ cần người dùng duyệt

1. Phạm vi MVP, persona, mục đích cá nhân hay thương mại và nguồn/điều kiện thương mại được phép.
2. Refresh mỗi 7 ngày, timezone, ngân sách và ai được khởi chạy; retention 7 ngày là cấu hình tách riêng.
3. Nếu refresh lỗi: xóa nghiêm ngặt sau 7 ngày hay giữ active stale có grace; retention baseline, raw, audit và kết quả user lưu khác nhau.
4. Taxonomy, mapping `home_goods`, reviewer và tiêu chí đánh giá thuật toán/quality.
5. AI provider/model/tác vụ, quyền gửi dữ liệu, token/chi phí, platform key hay BYOK, secret manager và quyền tenant.
6. Worker/scheduler/hosting, IAM least-privilege, SLO, backup/restore, chính sách quota, trả phí và điều kiện chuyển khỏi Vercel Hobby/Spark.
7. Các nguồn/evidence cần thiết cho Product Research, chủ sở hữu nội dung, IP/brand và workflow review; không tự biến attention thành doanh số.

**Đánh giá Bước 1:** PASS cho audit và kiểm thử offline theo phạm vi đã yêu cầu; an toàn tuyệt đối của lịch sử secret, điều kiện cloud hiện tại và production readiness **chưa được chứng minh**. Có thể bắt đầu Bước 2 với fixture synthetic sau khi xác nhận điều kiện trên, không cần khởi động Giai đoạn 2 T1–T5 hay cloud trong Bước 1.
