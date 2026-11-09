# Hướng dẫn phát triển TrendScout Giai đoạn 2 — Từ Demo đến MVP

**Trạng thái:** đề xuất để lần lượt thực hiện và nghiệm thu; chưa phê duyệt kiến trúc thương mại hoặc bắt đầu triển khai. **Cập nhật:** 09/11/2026. **Đối tượng:** chủ sản phẩm/BA, dev và người vận hành. Tài liệu này bổ sung, không thay thế [hướng dẫn demo](HUONG_DAN_CAC_BUOC_HOAN_THANH_DEMO.md), [thiết kế demo](TREND_KEYWORD_DEMO_BUSINESS_SYSTEM_DESIGN.md), [requirement BA/dev](REQUIREMENTS_TECH_BA_DEV.md), [thiết kế tổng thể](TrendScout_Phan_Tich_Nghiep_Vu_Thiet_Ke_Tong_The.docx) và [nghiệm thu demo](Reports/BAO_CAO_NGHIEM_THU_DEMO.md). Requirement BA/dev còn mô tả hệ thống tham chiếu cũ: không coi đó là code hiện tại.

**Quy ước báo cáo:** từ Bước 5, lưu báo cáo tại `Documents/Reports/BAO_CAO_BUOC_<N>_GIAI_DOAN_2.md`.

**Cách dùng:** thực hiện từng bước ở mục 9 theo phụ thuộc; kiểm tra diff và báo cáo trước khi chuyển bước. Các prompt là nhiệm vụ riêng, không tự gom thành một lần refactor. Mọi bước đều tuân thủ AGENTS.md và quy tắc chung mục 8; không tự deploy, bật billing, tạo key, sửa dữ liệu thật hoặc gọi nguồn live nếu chưa được cho phép riêng. Chi phí, quota và điều khoản bên ngoài phải kiểm tra lại tại lúc thực hiện.

## 1. Mục tiêu, MVP và giới hạn

Mục tiêu là tiến từ demo một nguồn sang **MVP có thể kiểm thử và vận hành an toàn**, rồi chuẩn bị khả năng thương mại: dữ liệu có nguồn gốc, nhiều thuật toán có thể giải thích, cập nhật có kiểm soát, phân loại có bằng chứng, và dashboard dễ dùng. Tín hiệu Wikimedia chỉ biểu thị sự chú ý tới các trang trong top theo phạm vi nguồn; không chứng minh doanh số, nhu cầu mua hay toàn bộ trend tại Mỹ.

**MVP tối thiểu đề xuất:** Wikimedia tiếp tục hoạt động; kiến trúc module nhỏ với contracts; taxonomy đa nhãn có `unclassified`; cleaning theo nguồn; algorithm engine chạy scoring demo cùng ít nhất một chiến lược đủ điều kiện và báo `SKIPPED` cho phần còn lại; evaluation ban đầu; shared dataset, bảo vệ quota, job/scheduler/retention được nghiệm thu an toàn; dashboard có xem kết quả, trạng thái và so sánh; auth/RBAC phù hợp phạm vi triển khai. AI và Product Research có thể vận hành ở chế độ tắt/chưa cấu hình nếu ngân sách hoặc quyền nguồn chưa được chấp thuận. Trước khi mời khách hàng phải hoàn thành các cổng bảo mật, tenant và điều khoản ở bước 20–21.

**Có thể hoãn:** nguồn Reddit/YouTube/Bluesky/News thật; nhiều AI provider; dịch hàng loạt; NER/embedding lớn; thuật toán thống kê nâng cao; BYOK; cơ sở dữ liệu mới; thanh toán, subscription và gợi ý sản phẩm tự động. Không tạo dữ liệu cho lĩnh vực rỗng.

## 2. Hiện trạng đã đối chiếu

| Thành phần | Bằng chứng / giới hạn |
|---|---|
| Backend | FastAPI Python 3.12; adapter Wikimedia → cleaning → keyword/classification → scoring v0.1 → Firestore; API health, manual run, run/detail/list keyword. |
| Demo | [Báo cáo Bước 10](Reports/BAO_CAO_NGHIEM_THU_DEMO.md) ghi một run live Firestore: 7.737 observation, 968 keyword, 226 document nội dung và khoảng 232 writes gồm trạng thái. Khoảng 9.412.755 byte JSON là nội dung, không phải tổng Firestore storage. |
| Dashboard | HTML/CSS/JS cùng origin; lọc, tìm, phân trang, chi tiết; nhập run ID. Có kiểm thử trình duyệt live theo báo cáo. Không suy ra đã có danh sách toàn bộ run. |
| Vận hành | `./start.sh` local; manual run dùng `HostRunLock` một host và recovery lúc startup; không phải distributed lock. Firestore Admin phía server chịu IAM, không dựa vào client Security Rules. |
| Vercel | Code chuẩn bị read-only: Basic auth, OIDC/WIF theo request, POST bị chặn và không recovery/job; test offline. Chưa có bằng chứng build/deploy Vercel, OIDC/WIF hoặc IAM read-only live. Không coi đã xác minh. |
| Còn thiếu | Scheduler độc lập, tenant/auth sản phẩm, secret management, nguồn ngoài Wikimedia, engine đa thuật toán, AI provider thực, taxonomy tổng thể, Product Research và bảo đảm vận hành thương mại. |

## 3. Kiến trúc mục tiêu — modular monolith

Phân tách boundary theo lợi ích, có thể giữ đường dẫn cũ tạm thời. Python package gợi ý, không tạo folder rỗng hoặc class chỉ để thể hiện pattern:

```text
Backend/app/
  api/                   # routes, auth, chuyển đổi request/response
  application/           # use cases, orchestration, ports, transaction boundary
  domain/                # entity, value object, job states, contracts, không phụ thuộc SDK
  sources/
    wikimedia/           # chuyển dần adapter/validation/metadata hiện tại
    reddit/              # khi được duyệt: contract + implementation riêng
    youtube/
    bluesky/
    news/
  processing/            # pipeline, shared normalize/dedup; source rules ở nguồn
  algorithms/            # interface, registry, strategies và evaluation dùng chung
  taxonomy/              # danh mục, version, bằng chứng, mapping
  ai/                    # provider port, policy, cache, usage; secrets chỉ server
  research/              # relevance, evidence, ideas, review tách trend
  infrastructure/        # firestore repository, scheduler/lease, SDK adapters
  workers/               # entrypoint worker/scheduler độc lập với HTTP
  settings.py, main.py   # configuration và composition root / DI
Frontend/                # UI, gọi API, không chứa secret
```

Các folder nguồn tương lai là **chỗ dự kiến**, chỉ thêm implementation sau khi có quyền và contract hợp lệ. Dependency hướng vào trong:

```text
Dashboard → API → Application/use cases → Domain + ports
                             ↑                 ↑
         worker/scheduler → composition root ← adapters: sources, storage, AI
                            ↓
               processing → algorithms + taxonomy
```

- **Clean Architecture/SOLID/DI:** use case nhận các port qua constructor/factory; domain không import Firebase, HTTP hay SDK. Tránh hierarchy sâu và framework DI nếu chưa cần.
- **Adapter:** mỗi nguồn tự xử lý xác thực, retry, timestamps, rate/terms và ánh xạ canonical. Adapter không đổi ý nghĩa metric.
- **Strategy + Registry/Factory:** registry có danh tính/version và điều kiện chạy; strategy độc lập; factory chỉ lắp cấu hình đã cho phép, không tự bật thuật toán.
- **Repository:** port lấy/lưu dataset, run, result, lease; implementation Firestore hoặc fake. Không lộ cấu trúc SDK qua domain.
- **Pipeline/Chain:** từng bước trả accepted/rejected/evidence; source-specific rule cắm riêng, lỗi có policy rõ; không bắt mọi record qua AI.
- **State machine:** chuyển trạng thái job hợp lệ, CAS/transaction và fencing bảo vệ active dataset. Nhiều host không dùng `flock` local làm lock chung.

### 3.1. Contract đề xuất

| Port/contract | Input → output và bất biến |
|---|---|
| `SourceAdapter` | `fetch(scope, window, cursor, budget) → SourceBatch(records, snapshots, errors, provenance)`; phân biệt 404/429, ngày thiếu, không top; cancellation/retry có giới hạn. |
| `CanonicalObservation` | source, scope, entity gốc/ID, project, timestamp, metric `{name,value,unit,kind}`, raw reference, chất lượng và license. `0`, `null`, missing, unavailable và censored khác nhau. |
| `ProcessingStage` | batch + context/version → accepted, rejected lý do, quality flags và thống kê đối soát; không âm thầm mất bản ghi. |
| `AlgorithmStrategy` | version + input window/metric/source + capabilities → `AlgorithmResult` chứa entity ID, metric, window, status `COMPLETED/SKIPPED/FAILED`, score nullable, evidence, flags, reason; deterministic nếu có seed/config. |
| `DatasetRepository` | tạo bản nháp immutable, đọc theo version, lưu shards, publish bằng transaction/CAS; delete theo namespace và checkpoint; fake tuân thủ cùng contract. |
| `AIProvider` | tác vụ có schema + budget + dữ liệu tối thiểu → output đã validate, model/version, usage, cost ước tính và lỗi có loại; không trả key. |
| `JobCoordinator` | schedule occurrence + idempotency key → job state; lease có expiry, heartbeat, fencing token và owner; chỉ worker hợp lệ được publish. |

**Migration:** chụp golden fixture và output v0.1 → thêm port bao ngoài code hiện có → chuyển một adapter/use case mỗi lần → shadow chạy offline so sánh → chuyển dependency injection → giữ route, field, status và Firestore cũ qua compatibility mapper → rollback bằng config. Không thay công thức scoring v0.1, entity ID hay kết quả đã lưu mà không có migration/version được duyệt. Chỉ gỡ lớp cũ sau regression và đối chiếu run cũ.

## 4. Cleaning đa lớp và bảo toàn dữ liệu

Mỗi observation giữ raw hoặc reference theo quyền lưu, source, license, thời gian, `entity_id` gốc, metric và transformation version. Pipeline có thể bỏ qua các bước đắt khi không phù hợp; mọi trường hợp skipped có lý do:

1. Schema validation (Pydantic nếu cần): kiểu, timestamp, required, số hữu hạn; từ chối sai schema có report.
2. Source validation: đơn vị, country scope, top-censored, retry/coverage; không suy US authors hay purchase intent.
3. Unicode NFC và normalize text/title theo nguồn; giữ raw, không tự giải mã hai lần.
4. Language detection với confidence và `unknown` cho title ngắn/tên riêng; không loại ngoại ngữ.
5. Rule nguồn có version và evidence: ví dụ namespace kỹ thuật Wikimedia, không áp dụng danh sách đó cho Reddit.
6. Exact dedup theo source identity + scope + observation window; liên kết provenance. Không gộp project/ngày khác.
7. Near-duplicate chỉ tạo candidate, hạn chế theo source/language/time và ngưỡng có kiểm thử; mặc định không gộp entity/mất observation tự động.
8. Spam/noise: rule/risk flag và quarantine, có lý do/review, không xóa im lặng.
9. AI translation tùy chọn khi hữu ích và được phép; budget guard trước khi gửi.
10. Entity extraction/enrichment: bảo toàn entity ID và provenance, tách thực thể/candidate, review khi mơ hồ.
11. Quality evaluation: coverage, missing/censored, rejection/duplicates theo ngày/nguồn; `raw = accepted + rejected + duplicate` theo định nghĩa report đã chốt.

| Kỹ thuật | Dùng khi / dữ liệu cần | Giới hạn, nguy cơ và chi phí |
|---|---|---|
| Pydantic, Unicode | validation và text ở mọi nguồn | Không quyết định tính đúng nghiệp vụ; normalize có thể đổi cách hiển thị, giữ raw. |
| RapidFuzz/Jaro-Winkler | candidate tiêu đề gần giống, ít dữ liệu | Tên ngắn/trùng thương hiệu dễ false positive; CPU tăng theo số cặp, chặn khối/candidate. |
| MinHash/LSH | nhiều văn bản đủ dài, near-duplicate theo token | Tốn chỉ mục/bộ nhớ, phụ thuộc ngưỡng, bỏ sót paraphrase và nhầm template; không phù hợp title quá ngắn. |
| Language detection | văn bản đủ dài, model confidence đã đánh giá | Tên riêng, code-switching dễ sai; `unknown` là kết quả hợp lệ. |
| NER/rule-based | văn bản có ngữ cảnh hoặc từ điển đáng tin | Phụ thuộc ngôn ngữ/domain, ambiguous; cần bộ test gán nhãn và CPU. |
| Embeddings/similarity | semantic candidate/retrieval khi có corpus, model và budget | Latency/storage/API cost; similarity không chứng minh cùng entity; version/vector và threshold phải được kiểm định. |
| AI-assisted cleaning | trường hợp không chắc, cần review, được phép gửi | Có thể bịa/xóa ngữ nghĩa; token cost, PII/license và lỗi provider; không là nguồn sự thật. |

**Translation contract:** phát hiện ngôn ngữ → chọn record thật sự cần → English cho NLP, Vietnamese chỉ khi hiển thị cần; giữ original/ID. Cache key gồm hash văn bản, source/target language, provider/model/version, prompt/policy version và tenant khi cần; không cache xuyên tenant nếu nội dung riêng. Lưu thời gian, language/confidence, chất lượng và `translation_status`; kiểm tra placeholders, độ dài, named entity và sample review; không tự sửa bản gốc. Timeout, retry có giới hạn, token ceiling, quota và maximum cost trước call; lỗi trả nguyên văn + cờ `TRANSLATION_UNAVAILABLE`, không chặn pipeline cốt lõi, trừ policy đã duyệt. Kiểm tra quyền gửi dữ liệu cho AI, tránh PII và dữ liệu bị cấm theo nguồn.

## 5. Algorithm Engine và đo chất lượng

Engine xem xét **mọi strategy đã đăng ký trong cấu hình phiên bản của run**; strategy chưa đăng ký/disabled không đồng nghĩa đã chạy. Registry `algorithm_id@version`, applicability function kiểm tra nguồn, unit, time cadence, missing/censoring và dữ liệu tối thiểu. Mỗi cặp entity/algorithm có kết quả riêng: `COMPLETED`, `SKIPPED` + lý do hoặc `FAILED` + lỗi đã khử bí mật; score nullable không biến skipped thành 0. Lưu `metric/unit`, window/current, sample count, baseline, config, evidence, flags và timestamp. Compare/rank phải chọn rõ strategy + version; score không so ngang nguồn/đơn vị nếu chưa chứng minh tính so sánh.

| Strategy (đề xuất) | Input/điều kiện áp dụng; output & lý do skip |
|---|---|
| Growth Ratio | current + baseline dương, cùng metric/scope; ratio và cửa sổ; skip zero/missing/incompatible. |
| Median Baseline | chuỗi observations cùng metric; median + số ngày; với demo tối thiểu 3/7 ngày quan sát, censored là thiếu chứ không là 0. |
| Robust Z / MAD | chuỗi đủ dài đại diện, median/MAD > 0; z và evidence; skip ít mẫu, MAD=0 hoặc censored gây thiên lệch không chấp nhận được. |
| EWMA | chuỗi time series cadence ổn định, alpha/version và minimum warmup; EWMA/delta; skip missing/cadence không hợp lệ. |
| CUSUM | chuỗi và baseline/warmup hợp lệ, tham số ngưỡng kiểm định; cảnh báo + evidence; skip thiếu baseline/vi phạm giả định. |
| Kleinberg Burst | chuỗi sự kiện hoặc counts tương thích và khoảng thời gian; burst interval/level, model parameters; skip timestamp/độ phân giải không đủ. |
| Change Point | chuỗi đủ dài, thuật toán/penalty đã chọn; điểm thay đổi + confidence/evidence theo model; skip khi quá ngắn hoặc missing không xử lý được. |
| Bayesian/count model | count hợp lệ, exposure/offset, giả định dispersion và prior đã duyệt; posterior/uncertainty; skip cận trên xấp xỉ hoặc censored nếu model không hỗ trợ. |
| Composite | chỉ kết hợp kết quả đã có cùng ý nghĩa, được chuẩn hóa/kiểm định và có weights/version; thiếu thành phần → skip hoặc policy được duyệt; không tự trộn views, likes, mentions. |
| NER / Topic classification | text + ngôn ngữ/ngữ cảnh, model/rule version; entity/category/evidence, không phải trend score; skip ngữ cảnh thiếu. |
| Semantic similarity | model version, vector tương thích, ngưỡng đã đánh giá; candidate + similarity, không xác nhận entity; skip thiếu vector/quyền/budget. |
| AI-assisted analysis | dữ liệu tối thiểu, provider được bật/quyền/budget; giải thích có citation ID và cờ review; skip disabled/budget/missing, không tự dựng sự thật. |

Scoring Wikimedia v0.1 giữ công thức và ngưỡng cũ; `views_ceil` là xấp xỉ, median chỉ trên ngày vào top có selection bias. Các chiến lược trên chỉ được bật sau test độc lập và applicability; nhiều thuật toán có thể `SKIPPED` toàn bộ với dữ liệu hiện tại, đó là kết quả đúng.

**Đánh giá:** tạo labeled dataset có nguồn/quyền và version, chia train/validation/test theo thời gian, không rò tương lai; có nhãn reviewer, cách xử lý bất đồng và sampled negatives. Báo Precision@K = số kết quả đúng trong K / K (xác định cách xử lý dưới K), false positives/K, coverage, tỷ lệ skip, so sánh volume-only và v0.1 trên cùng window; bootstrap/uncertainty khi mẫu đủ. Có thể báo chưa đủ bằng chứng thay vì công bố độ chính xác. Theo dõi drift và ngưỡng dựa trên data thực, không tối ưu bằng fixture synthetic.

## 6. Taxonomy và Product Research

Dùng ID ổn định không phụ thuộc tên hiển thị; taxonomy version, effective date và mapping cũ `home_goods → home_living` khi có migration đã duyệt. Ví dụ ID đề xuất:

| `topic_category` | Một số subcategory ID đề xuất |
|---|---|
| `entertainment` | `entertainment.film`, `entertainment.music`, `entertainment.tv` |
| `celebrity_public_figures` | `celebrity_public_figures.performers`, `celebrity_public_figures.politicians`, `celebrity_public_figures.creators` |
| `fashion` | `fashion.apparel`, `fashion.footwear`, `fashion.accessories` |
| `home_living` | `home_living.kitchen`, `home_living.decor`, `home_living.appliances` |
| `technology` | `technology.devices`, `technology.software`, `technology.ai` |
| `gaming` | `gaming.video_games`, `gaming.hardware`, `gaming.esports` |
| `beauty_personal_care` | `beauty_personal_care.skincare`, `beauty_personal_care.makeup`, `beauty_personal_care.haircare` |
| `sports` | `sports.teams`, `sports.events`, `sports.equipment` |
| `food_beverage` | `food_beverage.food`, `food_beverage.drinks`, `food_beverage.cooking` |
| `health_fitness` | `health_fitness.fitness`, `health_fitness.wellness`, `health_fitness.health` |
| `travel_lifestyle` | `travel_lifestyle.destinations`, `travel_lifestyle.transport`, `travel_lifestyle.lifestyle` |
| `business_finance` | `business_finance.companies`, `business_finance.markets`, `business_finance.personal_finance` |
| `news_politics` | `news_politics.events`, `news_politics.policy`, `news_politics.elections` |
| `other` | `other.confirmed` chỉ khi có bằng chứng chủ đề khác |
| `unclassified` | `unclassified.insufficient_context` |

`entity_type` (person, organization, product, place, event, work, unknown) độc lập `topic_category` đa nhãn; `commercial_relevance` (unknown/assessed + lý do/evidence) cũng độc lập. Rule, ML/embedding, AI có model/rule version, confidence, provenance, review status; thiếu bằng chứng giữ `unclassified`. AI chỉ đề xuất nhãn, không tự biến đề xuất thành nhãn chắc chắn.

**Product Research tách module:** Trend Detection → Commercial Relevance → Product Research → Product Ideas → Evidence & Review. Attention Score và Commercial Opportunity Score tách biệt, score thương mại chỉ có khi có metric/nguồn mua sắm hợp lệ, công thức và mức đủ dữ liệu được kiểm chứng. AI có thể tạo giả thuyết gắn `UNVERIFIED`, không tự tạo doanh số, giá, best-seller hay market demand. Evidence phải liên kết nguồn được phép, thời gian và trích dẫn; người dùng review. Đánh giá IP, nhãn hiệu, hình ảnh người nổi tiếng, giấy phép nguồn, hạn chế quảng cáo và khai thác thương mại trước khi công bố.

## 7. Firestore, quota, lịch và retention

### 7.1. Ước tính minh họa, không phải hóa đơn

Theo [Firestore Standard quota](https://firebase.google.com/docs/firestore/quotas) và [bảng giá Firebase](https://firebase.google.com/pricing) được đối chiếu 09/11/2026: 50.000 reads/ngày, 20.000 writes/ngày, 20.000 deletes/ngày, 1 GiB dữ liệu và 10 GiB network egress/tháng ở mức no-cost; kiểm tra lại database đủ điều kiện, đơn vị và ngày reset. Spark có thể chặn khi hết quota; không tự giả định tự động trả phí. TTL delete và một số tính năng yêu cầu billing; không dùng TTL trong Spark nếu chưa xác nhận. [Các giới hạn](https://firebase.google.com/docs/firestore/quotas): document tối đa 1 MiB, 40.000 index entries/document; index cũng tốn storage, query/index reads có thể tính phí theo [pricing](https://firebase.google.com/docs/firestore/pricing).

Giả định từ báo cáo cũ, **không phải dự toán được đo mới**: 232 writes/run, 226 content documents, 9.412.755 byte JSON/run. Một run/tuần ≈ 4,3 run/tháng ≈ 1.000 writes/tháng; một run/ngày ≈ 6.960 writes/tháng nhưng 232/ngày, trước retry/cleanup. 10 run/ngày ≈ 2.320 writes/ngày; 100 run/ngày ≈ 23.200, vượt 20.000 trước overhead. 10 run/ngày trong 7 ngày ≈ 658,9 MB JSON, chưa index/encoding, users hay bản cũ; không đồng nhất MB với GiB hoặc số storage thực. Với một run/tuần, chỉ hai phiên bản nội dung ≈ 18,8 MB JSON, nhưng baseline và các bản lỗi có thể tăng.

Đọc hiện tại: list load đủ 16 keyword shards + run document, tối thiểu xấp xỉ 17 document reads/lần trong giả định 16 shard tồn tại; 100 người × 10 lần tải ≈ 17.000 reads/ngày, chưa retries, metadata, search, detail, query/index reads hoặc cache. Không nhân số item hiển thị thành số reads mà không đo implementation. Lọc/tìm hiện tại đọc shards nhiều lần; cache server/shared có TTL, ETag, debounce và giới hạn request có thể giảm đọc nhưng phải kiểm tra phân quyền và invalidation. Egress phụ thuộc byte thực tế, region và đường mạng; ước lượng `tổng response byte × số lần đọc`, cộng traffic khác, không mặc định miễn phí.

Sharding giảm kích thước document và write count nhưng có thể tăng reads mỗi query và hotspot; index exemption cho raw/arrays không query chỉ sau đo và migration, giữ index thật sự cần. Khi users/số nguồn/tìm kiếm hoặc lịch sử tăng, đo usage theo ngày, p95 latency và chi phí, rồi so PostgreSQL cho query/transaction/tenant, object storage cho raw được phép lưu, analytics DB cho phân tích lớn; tất cả có chi phí/quyền/retention riêng, không chuyển chỉ vì pattern. Shared source dataset theo source/scope/window/version giúp một fetch phục vụ nhiều tenant khi license cho phép, tenant settings/queries tách biệt. Không hứa Spark đủ cho số người dùng chưa đo.

### 7.2. Weekly refresh và retention là hai cấu hình độc lập

Đề xuất `refresh_interval=7 ngày` và `retention=7 ngày` **chưa được phê duyệt**. Lịch chạy bằng scheduler thực bên ngoài process web (worker/managed scheduler hoặc máy Ubuntu luôn bật); không phụ thuộc dashboard mở, không dùng process chạy mãi trên Vercel Functions. Lựa chọn provider, quota và chi phí được xác nhận trước khi vận hành. `Start` kích hoạt lịch có giới hạn, `Pause` dừng dispatch lần tới, `Stop` ngăn occurrence mới và yêu cầu hủy hợp tác; không xóa run/không vòng lặp không giới hạn. Có max requests, timeout, cooldown và circuit breaker.

```text
schedule occurrence (UTC, idempotency key)
 → lease + fencing → FETCHING → CLEANING → ANALYZING → PERSISTING
 → VALIDATING → publish active pointer bằng CAS → COMPLETED
 → cleanup riêng theo namespace + checkpoint + retry
```

Các trạng thái job ngoài demo: `SCHEDULED`, `PAUSED`, `CANCELLING`, `CANCELLED`, `RETRY_WAIT`; mapping công khai với trạng thái API cũ phải version/compatibility rõ. Lease hết hạn, heartbeat, fencing token tăng dần và idempotency theo occurrence ngăn worker cũ publish sau khi worker mới tiếp quản. CAS chỉ publish bản đầy đủ, chất lượng và quota guard đạt; lỗi 404/429/AI/Firestore giữ nguyên active cũ, ghi lỗi, retry giới hạn và báo stale. AI optional failure không chặn core trừ policy đã duyệt. Restart/retry/replayed occurrence không tạo duplicate và không cho hai job publish.

Dữ liệu phải phân vùng: `analysis_datasets/{dataset_id}` cho snapshot/results tạm; raw/baseline có retention riêng theo license và yêu cầu 7 ngày lịch trước; `jobs`, audit/ops; `tenants/{id}/settings`, users, secrets reference/subscription; saved research của user theo policy riêng. Firestore xóa document cha **không tự xóa subcollection**. Cleanup chỉ xóa manifest/paths đã allowlist cho dataset quá hạn, theo trang và checkpoint, idempotent, giới hạn deletes, không xóa active, tenant, user, credentials hay database. Test giả lập failure/restart giữa từng trang; không chạm dữ liệu live khi chưa được duyệt.

**Xung đột cần quyết định:** nếu refresh thất bại sát ngày thứ 7, vừa xóa nghiêm ngặt đúng 7 ngày vừa giữ active cũ là bất khả thi. Mặc định *đề xuất để thảo luận*: giữ active làm fallback tối đa thêm 7 ngày, gắn `STALE` và cảnh báo; quá 14 ngày ẩn/trả unavailable và cleanup sau khi hết thời gian gia hạn; baseline giữ tối thiểu cửa sổ thực sự cần nếu license cho phép. Nếu chọn xóa nghiêm ngặt, không giữ fallback quá hạn, có thể không có dữ liệu; legal deletion yêu cầu nghiêm ngặt luôn ưu tiên. Đây không phải quyết định đã chốt. Nếu quota đầy giữa cleanup, giữ checkpoint, thông báo và tiếp tục khi có quota; không bao giờ tự mở billing.

## 8. AI, bảo mật và hosting

**AI Provider Abstraction:** adapter OpenAI, Gemini, provider khác chỉ sau khi xem điều khoản, địa điểm xử lý, retention/training, giá và dữ liệu được phép gửi. Mọi tác vụ translation, classification, NER, enrichment, trend explanation, commercial relevance, ideas, summary có JSON schema/structured output, validation, timeout, retry có backoff và phân loại lỗi, rate limit/concurrency, cache, log không chứa prompt nhạy cảm/key, audit ID, model/prompt/version, estimated vs actual usage và quota/budget guard. Prompt injection từ text nguồn là dữ liệu không đáng tin; không thực thi tool/URL do nó đề xuất. Không gọi AI chỉ để lấp dữ liệu trống.

**Keys:** platform-managed dùng secret manager backend, quay vòng/revoke và IAM theo service; chi phí nền tảng chịu. BYOK lưu ciphertext với KMS/envelope encryption hoặc secret manager, key riêng/tenant binding, RBAC, rotation, revocation, audit, xử lý delete và incident; key chỉ truyền HTTPS, không log, không trả lại raw (chỉ masked metadata), không localStorage/frontend bundle. Test connection cũng tiêu quota và cần rate/budget limit. Free tier, hosting ephemeral và dịch vụ quản lý key có thể không hỗ trợ đủ isolation/availability miễn phí; BYOK có thể hoãn. Link chính thức: [OpenAI API keys](https://platform.openai.com/docs/quickstart), [Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key). Chỉ tạo key ở bước tích hợp thật sau khi người dùng chấp thuận; không gửi key vào chat.

**Phân quyền sản phẩm:** identity + tenant membership từ server, RBAC user/operator/admin, authorize mọi read/write/job/secret theo tenant; shared public dataset chỉ có nội dung được phép, dữ liệu riêng không truy cập chéo. HTTP Basic/password demo và operator token không đủ cho sản phẩm nhiều khách. Có rate limiting, session/CSRF phù hợp, audit không secret, least-privilege IAM và backup/restore, quyền xóa. Security Rules client không giới hạn Admin SDK; tenant auth phía server phải được kiểm thử.

**Hosting:** local backend lâu dài phụ thuộc máy/mạng; Vercel Function theo request ngắn, không phải worker thường trực; scheduler đánh thức worker độc lập, durable queue/lease/state trong storage. AI calls có timeout, budget, idempotency và cancellation; Firestore là storage, không phải scheduler. Có thể khảo sát managed scheduler + container/job/queue hoặc host worker luôn bật với watchdog, chi phí và điều kiện gói tùy nhà cung cấp; không bắt buộc mua ngay và không hứa free chạy 24/7. [Vercel Hobby](https://vercel.com/docs/plans/hobby) chỉ cho mục đích cá nhân/phi thương mại; trước thương mại cần gói/host phù hợp. WIF/OIDC hiện chưa được xác nhận end-to-end; chỉ khẳng định sau khi deploy và kiểm tra IAM thực tế.

**Quy tắc chung cho mọi prompt:** đọc root và AGENTS liên quan, tài liệu này, source và test trước sửa; tôn trọng user changes. Giữ `./start.sh`, `/api/health`, API/run cũ và demo v0.1 trừ khi có quyết định migration riêng. Offline/fake trước; synthetic phải gắn nhãn. Không tự live fetch, deploy, ghi/xóa Firestore, tạo credential, bật billing hoặc gửi dữ liệu đến AI. Không đưa secret vào source/log/chat. Báo danh sách file, test, lỗi, quota/chi phí dự kiến, giả định và điều kiện chặn. Các bước chỉ triển khai phạm vi nêu trong prompt; việc cloud/chi phí luôn cần người dùng cho phép khi thực hiện.

## 9. Các bước triển khai — việc làm tay, prompt và nghiệm thu

### Bước 1 — Audit, baseline và quyết định phạm vi

**Mục tiêu/phụ thuộc:** thiết lập baseline; bắt đầu từ demo hiện tại. **Bạn làm thủ công:** xác nhận phạm vi MVP và nguồn dữ liệu được phép dùng, chọn ngân sách bằng 0 cho khảo sát offline. Không cung cấp secret.

**Prompt gửi AI/Codex:**

```text
Đọc AGENTS.md, Documents/HUONG_DAN_CAC_BUOC_HOAN_THANH_GIAI_DOAN_2.md,
các tài liệu demo, báo cáo nghiệm thu và repo. Audit kiến trúc, contracts API,
Firestore, test, security, các thay đổi chưa commit và trạng thái Vercel.
Ghi baseline version, hành vi và rủi ro, phân biệt legacy, live và synthetic;
đề xuất fixture golden được phép dùng và danh sách quyết định cần chủ dự án duyệt.
Không refactor, fetch live, sửa dữ liệu, tạo credential, deploy hay bật billing.
```

**Kiểm tra / Nghiệm thu:** chạy test backend hiện có từ `Backend/` và ghi số test thực tế, `bash -n start.sh setup.sh`, kiểm tra git diff; pass khi có bản đồ API/storage/scoring và phân loại evidence, fail nếu gọi fixture là live. **Rủi ro/chi phí:** không dự kiến phí; thiếu bằng chứng Vercel → đánh dấu chưa kiểm chứng, không tự live.

### Bước 2 — Golden regression và contract API

**Mục tiêu/phụ thuộc:** khóa hành vi trước migration; cần Bước 1. **Bạn làm thủ công:** duyệt dữ liệu fixture không chứa secret hoặc dữ liệu không có quyền lưu.

**Prompt gửi AI/Codex:**

```text
Dựa trên Bước 1 và AGENTS.md, tạo bộ regression offline cho demo TrendScout:
Wikimedia fixture hợp lệ/missing/censored, cleaning, classification, scoring
v0.1, route health/run/list/detail, pagination và dữ liệu Firestore cũ qua fake.
Chụp expected output có version và provenance, phân biệt synthetic với live;
bảo toàn timestamp/id theo contract, không gọi Firestore hoặc Wikimedia thật.
Không thay thuật toán/API hay refactor ngoài phạm vi test. Chạy toàn bộ test.
```

**Kiểm tra / Nghiệm thu:** test cũ+mới pass, golden tái lập, `0/null/missing/404/not-in-top` khác nhau; test phải phát hiện sai lệch cố ý. **Rủi ro/chi phí:** fixture thiên lệch; không có dữ liệu được phép → dùng synthetic, báo giới hạn, không bịa live.

### Bước 3 — Ports và composition root

**Mục tiêu/phụ thuộc:** DI/Clean Architecture tối thiểu; Bước 2. **Bạn làm thủ công:** xem sơ đồ và duyệt phạm vi file di chuyển.

**Prompt gửi AI/Codex:**

```text
Theo Giai đoạn 2 mục 3 và AGENTS.md, tách tối thiểu các port source,
repository và use case cần thiết từ backend hiện tại; composition root lắp
implementation cũ, dùng dependency injection. Không thay luồng, contract API,
scoring, storage, start.sh hoặc chế độ Vercel read-only; không tạo class thừa.
Chạy regression golden, health và toàn bộ tests; báo mapping cũ/mới và rollback.
Không live/deploy/ghi cloud.
```

**Kiểm tra / Nghiệm thu:** pass khi test và payload golden giống baseline, import domain không phụ thuộc SDK, local start và read-only không đổi; fail nếu API/schema thay âm thầm. **Rủi ro/chi phí:** lỗi import; không tốn cloud, dừng nếu không chứng minh tương đương.

### Bước 4 — Source Adapter framework và Wikimedia migration

**Mục tiêu/phụ thuộc:** một adapter thật sau port; Bước 3. **Bạn làm thủ công:** kiểm tra điều khoản Wikimedia và User-Agent; chưa tạo API khác.

**Prompt gửi AI/Codex:**

```text
Triển khai source adapter contract/registry cho TrendScout và chuyển Wikimedia
hiện có qua adapter riêng theo mục 3, giữ project+article, nguồn gốc, timestamp,
404/429, quota và censored. Định nghĩa capability/metadata để mở rộng Reddit,
YouTube, Bluesky, News sau này nhưng không kết nối hoặc dựng adapter giả.
Thêm contract tests, so sánh output fixture và v0.1 với baseline; không gọi live.
```

**Kiểm tra / Nghiệm thu:** fixture đối chiếu đủ records, 404/429 và retry giới hạn; registry không tự fetch nguồn mới. **Rủi ro/chi phí:** thay identity/limit làm sai baseline; thiếu quyền nguồn mới → không triển khai nguồn đó.

### Bước 5 — Repository và migration tương thích

**Mục tiêu/phụ thuộc:** tách storage có thể thay thế; Bước 3 và 4. **Bạn làm thủ công:** xác nhận không thay project/database live, duyệt mapping và rollback.

**Prompt gửi AI/Codex:**

```text
Theo tài liệu Giai đoạn 2, tạo repository port theo use case và bọc Firestore
repository hiện có; fake có cùng contract. Đọc run/shard schema demo bằng
compatibility mapper, không sửa document cũ hay thay IDs. Thêm tests read/write,
partial failure và restart qua fake/emulator; giữ API cũ và Vercel read-only.
Báo mapping, index dự kiến, migration/rollback; không ghi/xóa Firebase thật.
```

**Kiểm tra / Nghiệm thu:** fake/emulator pass, null/missing/0 và schema cũ giữ nguyên; lỗi ghi không báo COMPLETED. **Rủi ro/chi phí:** migration storage; không có emulator thì fake đủ cho offline nhưng không tuyên bố Firestore live pass.

### Bước 6 — Taxonomy và phân loại rule

**Mục tiêu/phụ thuộc:** taxonomy đa cấp/đa nhãn; Bước 4–5. **Bạn làm thủ công:** duyệt ID, tên nhóm và chính sách `other`/`unclassified` ở mục 6.

**Prompt gửi AI/Codex:**

```text
Triển khai taxonomy versioned ở mục 6 với IDs ổn định, subcategory, entity_type,
topic_category đa nhãn và commercial_relevance độc lập. Chuyển classification
rule hiện có qua mapper tương thích fashion/home_goods; giữ evidence, phiên bản,
review status và unclassified khi thiếu chứng cứ. Giữ mọi keyword trong All.
Viết test nhầm lẫn, đa nhãn, Unicode, mapping cũ, không tự gọi AI hay API mới.
```

**Kiểm tra / Nghiệm thu:** mọi category/subcategory có ID duy nhất; old filter còn hoạt động; ví dụ không đủ chứng cứ không biến thành `other`. **Rủi ro/chi phí:** phân loại sai hoặc mất nhãn cũ; thiếu taxonomy được duyệt → chỉ đề xuất, không migration thật.

### Bước 7 — Cleaning pipeline, schema và source rules

**Mục tiêu/phụ thuộc:** validation, normalize, dedup theo nguồn; Bước 4–6. **Bạn làm thủ công:** xem policy loại/quarantine và mẫu dễ loại nhầm.

**Prompt gửi AI/Codex:**

```text
Tạo pipeline cleaning có stage contracts theo mục 4: schema, source validation,
Unicode/text, source rules, exact dedup, quality report. Giữ raw/provenance,
metric, ngày và ID; không đổi missing/censored thành 0 hoặc gộp project/ngày.
Chuyển Wikimedia cleaning hiện tại từng phần, không thêm near-duplicate hay AI
mặc định. Test reconciliation, technical pages, Unicode và regression demo.
Chỉ dùng fixture, không live.
```

**Kiểm tra / Nghiệm thu:** accepted/rejected/duplicates đối soát được, lý do/version rõ; các kết quả golden không thay đổi ngoài bug được chứng minh. **Rủi ro/chi phí:** false positive; không tự thay rule nguồn chưa được xem xét.

### Bước 8 — Language, near-duplicate và quality

**Mục tiêu/phụ thuộc:** stage tùy chọn có thể giải thích; Bước 7. **Bạn làm thủ công:** duyệt ngưỡng thận trọng và ngân sách CPU, chọn phạm vi ngôn ngữ thử nghiệm.

**Prompt gửi AI/Codex:**

```text
Cho TrendScout thêm language detection confidence/unknown và near-duplicate
candidate generation tùy chọn; đánh giá RapidFuzz/Jaro-Winkler hoặc MinHash/LSH
với bộ dữ liệu phù hợp. Không tự gộp entity hay xóa bản ghi dựa trên similarity;
thêm noise flags, quality report, provenance, ngưỡng có version và tests false
positive, ngôn ngữ ngắn/code-switching. Chạy offline, báo phụ thuộc/CPU và skip.
```

**Kiểm tra / Nghiệm thu:** không mất observation, tên giống khác entity không gộp; unknown hợp lệ. **Rủi ro/chi phí:** CPU/memory và nhầm ngôn ngữ; thiếu corpus/model/license → stage SKIPPED, không tự tải model/thuê dịch vụ.

### Bước 9 — Algorithm Engine và scoring v0.1

**Mục tiêu/phụ thuộc:** engine dùng chung; Bước 2, 4, 7. **Bạn làm thủ công:** duyệt ID/version và quy tắc skip.

**Prompt gửi AI/Codex:**

```text
Tạo AlgorithmStrategy/registry/runner theo mục 5: duyệt mọi strategy đã đăng
ký cho run, applicability và result COMPLETED/SKIPPED/FAILED có version, lý do,
evidence, quality flags. Bọc scoring Wikimedia v0.1 giữ đúng công thức/status/API
và thêm Growth Ratio, Median Baseline nếu tái dùng được; không tính score giả,
không trộn metric/nguồn. Unit và integration tests độc lập, golden regression,
missing/zero/censored, hai project và deterministic. Không live.
```

**Kiểm tra / Nghiệm thu:** v0.1 khớp fixture, mỗi strategy registered có kết quả hoặc skip có lý do; exception một strategy không giả thành score. **Rủi ro/chi phí:** hiệu năng theo entity×algorithm; thiếu chuỗi hợp lệ → SKIPPED.

### Bước 10 — Chiến lược thống kê có chọn lọc

**Mục tiêu/phụ thuộc:** thử MAD/EWMA và hợp đồng chiến lược nâng cao; Bước 9. **Bạn làm thủ công:** duyệt tối đa hai thuật toán đầu tiên và điều kiện dữ liệu.

**Prompt gửi AI/Codex:**

```text
Trong algorithm engine TrendScout, triển khai độc lập Robust Z/MAD và EWMA
với applicability, tối thiểu mẫu, cadence, censoring, MAD=0 và tham số có version;
test độc lập bằng chuỗi synthetic có expected thủ công. Thiết kế spec/điều kiện
SKIPPED cho CUSUM, Kleinberg, change point và Bayesian count, không giả định chúng
phù hợp top-per-country hoặc bật mặc định. Giữ v0.1 và kết quả từng strategy riêng.
Không dùng dữ liệu live, AI hay thay đổi API công khai ngoài contract đã duyệt.
```

**Kiểm tra / Nghiệm thu:** kết quả toán học đối chiếu, dữ liệu không phù hợp bị skip, test v0.1 pass. **Rủi ro/chi phí:** censored gây sai inference; thiếu chuỗi đại diện → không phát hành strategy cho nguồn đó.

### Bước 11 — Evaluation và so sánh thuật toán

**Mục tiêu/phụ thuộc:** đo chất lượng trước khi bật rộng; Bước 9–10. **Bạn làm thủ công:** chọn mẫu và reviewer, xác nhận quyền dữ liệu, tiêu chí đúng/sai.

**Prompt gửi AI/Codex:**

```text
Tạo evaluation offline cho TrendScout theo mục 5: format labeled dataset có
nguồn/version/reviewer, tách theo thời gian, Precision@K, false positives,
coverage, tỷ lệ SKIPPED, so sánh volume-only, v0.1 và strategy phù hợp.
Cho phép chọn algorithm/version để xếp hạng độc lập; không kết hợp metric trái
đơn vị hoặc công bố accuracy từ synthetic. Test rò tương lai và K lớn hơn mẫu.
Không gọi provider, không sửa dữ liệu live.
```

**Kiểm tra / Nghiệm thu:** các chỉ số tính đúng trên fixture, báo số mẫu và bất định; thiếu nhãn → báo chưa đánh giá, không tạo nhãn. **Rủi ro/chi phí:** công sức review và selection bias; cần quyền dữ liệu trước dùng mẫu thật.

### Bước 12 — Storage optimization và shared dataset

**Mục tiêu/phụ thuộc:** đo và giảm quota; Bước 5, 9. **Bạn làm thủ công:** kiểm tra Usage Console và giá/giới hạn hiện tại, chọn ngưỡng ngân sách; không bật billing.

**Prompt gửi AI/Codex:**

```text
Audit đường đọc/ghi Firestore TrendScout theo các scenario tại mục 7; tính reads,
writes, deletes, bytes, shard/index và giả định từ fixture, dự toán peak users.
Thiết kế shared dataset, cache, invalidation, immutable result và index exemptions;
chỉ tối ưu phần được chứng minh cần thiết qua fake/emulator, giữ read-only và
API cũ. Có quota guard, báo số đo và rollback; không ghi/xóa cloud hoặc đổi index thật.
```

**Kiểm tra / Nghiệm thu:** báo trước/sau kèm giả định; không rò dữ liệu tenant/cache, không mất dữ liệu hay phá query; document dưới giới hạn. **Rủi ro/chi phí:** cache stale, index build và đọc đột biến; thiếu số liệu → dừng ở dự toán, không khẳng định Spark đủ.

### Bước 13 — Job state machine và distributed lease

**Mục tiêu/phụ thuộc:** điều phối không phụ thuộc một host; Bước 5, 12. **Bạn làm thủ công:** duyệt điều kiện hủy, timeout, số retries và chi phí tối đa.

**Prompt gửi AI/Codex:**

```text
Thiết kế và triển khai state machine job TrendScout theo mục 7: occurrence key,
transition validation, distributed lease transaction/CAS, heartbeat, expiry,
fencing, idempotency và cooperative cancellation; bảo toàn API/state demo qua
adapter. Test fake/emulator hai worker, race, restart, stale owner, retry, quota
và crash từng trạng thái. Không bật scheduler, không chạy worker hay ghi cloud thật.
```

**Kiểm tra / Nghiệm thu:** chỉ một owner được publish dù lease hết hạn; retry không nhân bản job; lỗi ghi không COMPLETED. **Rủi ro/chi phí:** distributed consensus sai gây duplicate; thiếu emulator/atomic transaction proof → không cho multi-host live.

### Bước 14 — Refresh scheduler và atomic publish

**Mục tiêu/phụ thuộc:** lịch không cần Dashboard; Bước 12–13. **Bạn làm thủ công:** chọn host/scheduler phù hợp và ngân sách; duyệt timezone, start/pause/stop, chính sách fallback. Chưa bật lịch thật.

**Prompt gửi AI/Codex:**

```text
Triển khai scheduler interface và worker orchestration cho refresh 7 ngày theo
mục 7, cấu hình interval độc lập retention, không phụ thuộc Vercel process hoặc
trình duyệt. Mỗi occurrence có budget, cooldown, idempotency; fetch → validate →
process → persist draft → quality gate → CAS active pointer; hỗ trợ pause/stop,
retry bounded và báo stale. Test bằng fake clock, fake provider/storage cho
404/429/AI fail/quota/restart/duplicate/race; không kích hoạt lịch hoặc nguồn thật.
```

**Kiểm tra / Nghiệm thu:** refresh thất bại giữ active theo policy đã duyệt, không publish draft lỗi; fake clock 7 ngày và pause/stop đúng. **Rủi ro/chi phí:** scheduler/worker có thể trả phí hoặc dừng khi host ngủ; chưa chọn hosting/quota → không bật tự động.

### Bước 15 — Retention và cleanup an toàn

**Mục tiêu/phụ thuộc:** xóa từng dataset có kiểm soát; Bước 14 và quyết định retention. **Bạn làm thủ công:** quyết định strict delete hay grace, baseline, saved research, quyền source và cấp riêng quyền xóa khi cho phép chạy thật.

**Prompt gửi AI/Codex:**

```text
Theo retention đã được chủ dự án xác nhận và mục 7, xây cleanup job độc lập,
namespace allowlist, manifest subcollection, pagination/checkpoint/idempotency,
quota limit, audit và fail-safe; không xóa active trong policy cho phép, users,
settings, secrets, subscription hay toàn database. Xử lý crash giữa batch,
quota 429/retry và concurrent publish bằng fake/emulator. Không bật TTL, không
xóa dữ liệu thật, không tạo scheduler. Báo rollback/điều kiện dừng.
```

**Kiểm tra / Nghiệm thu:** không sót subcollection trong manifest, resume an toàn, không xóa ngoài scope hoặc active; strict policy và fallback có test riêng. **Rủi ro/chi phí:** deletes, mất dữ liệu không hồi phục; chưa chốt policy/quyền → chỉ thiết kế và test fake, không live.

### Bước 16 — AI provider abstraction và guard

**Mục tiêu/phụ thuộc:** hạ tầng AI an toàn, chưa gọi thật; Bước 5, 12. **Bạn làm thủ công:** chọn tác vụ và giới hạn ngân sách; chưa tạo key nếu chỉ test fake.

**Prompt gửi AI/Codex:**

```text
Tạo AIProvider port/registry, fake provider và policy cho TrendScout: structured
output/schema validation, timeout, retry bounded, concurrency/rate limit,
cache version/tenant, cost estimate, token budget, usage và log đã khử secret.
Hỗ trợ model/provider metadata, xử lý prompt injection như untrusted data;
thiết kế adapter OpenAI/Gemini, không gọi provider thật hoặc lưu key plaintext.
Viết test malformed output, timeout, quota, budget, redaction và cross-tenant.
```

**Kiểm tra / Nghiệm thu:** fake có thể mô phỏng failure, không trả key/log payload nhạy cảm, budget cạn chặn call; chưa có key → vẫn test offline. **Rủi ro/chi phí:** sau này token, secret manager, logs; không tạo credential hay call trả phí.

### Bước 17 — AI translation có điều kiện

**Mục tiêu/phụ thuộc:** dịch phục vụ NLP/UI; Bước 8 và 16. **Bạn làm thủ công:** duyệt quyền gửi nguồn, ngôn ngữ, model và budget; chỉ khi muốn live mới tạo key trong kênh bí mật.

**Prompt gửi AI/Codex:**

```text
Thêm stage AI translation tùy chọn theo mục 4 vào TrendScout: detect language,
lọc dữ liệu cần dịch, English cho NLP, Vietnamese nếu hiển thị cần; giữ original,
entity ID/provenance. Cache hash+language+model/prompt/tenant, lưu version/usage,
kiểm tra quality/placeholders/named entities và review flags. Timeout/invalid/
budget trả original với TRANSLATION_UNAVAILABLE. Test fake, multilingual,
cache hit, lỗi và không đổi ID. Không gửi dữ liệu thật hay gọi API có phí.
```

**Kiểm tra / Nghiệm thu:** translation không thay metric/ID, cache đúng scope, fallback không giả thành bản dịch; **Rủi ro/chi phí:** token/PII/bản dịch sai. Thiếu consent/license/key/quota → `SKIPPED`, không gọi live.

### Bước 18 — AI classification và semantic enrichment

**Mục tiêu/phụ thuộc:** đề xuất nhãn và entity có evidence; Bước 6, 11, 16. **Bạn làm thủ công:** xác nhận schema, nhãn review và ngưỡng chất lượng, quyền sử dụng dữ liệu.

**Prompt gửi AI/Codex:**

```text
Triển khai AI classification/NER/semantic enrichment tùy chọn trong TrendScout,
ưu tiên đề xuất cho unclassified; kết quả phải có evidence, provider/model/version,
confidence theo contract, review status và fallback rule/unknown. Không tự gộp
entity, không biến similarity/AI thành trend score. Dùng fake provider, test
invalid schema, hallucinated evidence, multi-label, tenant, budget và fallback;
giữ tương thích taxonomy và không gọi AI thật.
```

**Kiểm tra / Nghiệm thu:** invalid evidence không được publish là sự thật; test rule cũ và unclassified pass. **Rủi ro/chi phí:** hallucination, thiên lệch, token/vector storage; thiếu nhãn review hoặc quyền → không bật tự động.

### Bước 19 — Product Research có bằng chứng

**Mục tiêu/phụ thuộc:** tách research khỏi trend; Bước 6, 11, 16. **Bạn làm thủ công:** chọn loại bằng chứng thương mại, chính sách IP, phạm vi thị trường và ngưỡng review; chưa cấp key.

**Prompt gửi AI/Codex:**

```text
Thiết kế và triển khai module Product Research riêng theo mục 6: commercial
relevance → evidence → ideas → review, attention score tách opportunity score.
Không bịa giá, doanh số, nguồn, demand hoặc sản phẩm bán chạy; thiếu bằng chứng
trả unknown/UNVERIFIED. Source/license/time, IP/brand/person risks và approval
phải hiển thị. Test bằng synthetic và fake AI, không tích hợp nguồn thương mại,
không gọi AI thật hoặc đưa gợi ý chưa review thành kết luận.
```

**Kiểm tra / Nghiệm thu:** output không có citation không hợp lệ thành fact; tách score và provenance; test data không được báo live. **Rủi ro/chi phí:** IP/nguồn thương mại, model cost; không có nguồn hợp lệ → chỉ giả thuyết hoặc để module tắt.

### Bước 20 — Dashboard, AI Settings và chuẩn bị multi-tenant

**Mục tiêu/phụ thuộc:** UX vận hành và bảo mật; Bước 6, 9, 13–19 theo tính năng bật. **Bạn làm thủ công:** duyệt vai trò, luồng UX, mô hình platform key hay BYOK và công cụ secret phù hợp; không nhập key vào chat.

**Prompt gửi AI/Codex:**

```text
Theo AGENTS.md root/Frontend và mục 8 tài liệu Giai đoạn 2, xây dashboard responsive
cho Sources, Runs, Algorithm Comparison, Trend Details, category/subcategory,
Product Research, Weekly Refresh, quality, AI usage/cost và Settings. Start/Stop/
Pause gọi job API có auth/cooldown/budget, không tạo vòng fetch; hiển thị stale,
empty, SKIPPED và unverified. Thiết kế/triển khai theo phạm vi được duyệt auth,
RBAC, tenant isolation; AI settings chọn provider/model, enable, budget, test
connection, hướng dẫn key chính thức, chỉ masked metadata. Test fake browser/API,
a11y, unauthorized/cross-tenant và không lưu/echo raw key. Không live/deploy.
```

**Kiểm tra / Nghiệm thu:** desktop/mobile, bàn phím, lỗi/empty, compare, phân trang, nhiều tenant và raw key không bị lộ; chức năng chưa có backend hiển thị disabled rõ. **Rủi ro/chi phí:** auth/secret manager, test connection và hosting; chưa có secret manager/RBAC đủ an toàn → tắt BYOK, không phát hành cho khách.

### Bước 21 — E2E, production readiness và cổng thương mại

**Mục tiêu/phụ thuộc:** nghiệm thu MVP; Bước 1–15, 20 và các bước AI/research đã chọn. **Bạn làm thủ công:** xác nhận decisions mục 10, đo quota/giá, nguồn hợp lệ, hosting phi thương mại hoặc gói phù hợp; cấp quyền test live hạn chế riêng nếu muốn.

**Prompt gửi AI/Codex:**

```text
Thực hiện kiểm thử E2E và production readiness TrendScout theo mục 10–11, đọc
AGENTS và báo cáo các bước đã nghiệm thu. Offline test toàn pipeline/API/UI,
multi-tenant/auth, job race/restart, quota/404/429, partial write, stale fallback,
cleanup resume và v0.1 regression; kiểm tra CI, dependency, secrets, backup/
restore giả lập, accessibility và observability. Lập kế hoạch live có budget,
quyền, rollback và tiêu chí dừng; chỉ chạy live nếu được cho phép riêng. Không
bật billing, deploy, sửa/xóa dữ liệu thật hoặc tạo key. Báo pass/fail/blocked và
bằng chứng từng tiêu chí, không gọi fake là live.
```

**Kiểm tra / Nghiệm thu:** mọi tiêu chí áp dụng mục 11 có bằng chứng, test cũ pass; run live, IAM, host, restore, load hoặc bảo mật chưa kiểm chứng phải ghi `BLOCKED`, không gọi production-ready. **Rủi ro/chi phí:** cloud reads/writes/deletes, API, AI, worker và egress; không đủ quyền/budget → dừng live, vẫn hoàn tất báo cáo offline.

### Bước 22 — Tích hợp một AI provider thật (tùy chọn sau Bước 16–18)

**Mục tiêu/phụ thuộc:** kiểm chứng provider adapter thật sau khi fake, quota guard và security đã pass. Có thể làm trước Bước 21 nếu MVP đã chọn AI; bỏ qua nếu AI nằm ngoài MVP. **Bạn làm thủ công:** xem trang chính thức và bảng giá, chọn OpenAI hoặc Gemini/model, giới hạn chi tiêu và dữ liệu được phép gửi; tạo key ở provider chỉ khi đã đồng ý phí; cấu hình secret manager/environment server và IAM, không gửi key vào chat. Xác nhận rõ số request và ngân sách live trước khi thử.

**Prompt gửi AI/Codex:**

```text
Đọc AGENTS.md, tài liệu Giai đoạn 2 mục 8 và kết quả Bước 16–18. Tích hợp một
provider AI [người dùng điền tên/model] qua AIProvider port, structured output,
validation, timeout/retry/rate limit, usage/cost, cache, redaction và quota guard.
Secret lấy từ secret store/server environment, không trả key cho frontend.
Chạy tests với mock và báo dự toán. Không tự gọi API live hoặc tạo credential;
nếu tôi đã cấp phép riêng một smoke test thì chỉ gửi dữ liệu synthetic tối thiểu,
trong số request và ngân sách tôi chỉ định; báo request/usage thực và lỗi, không
nói live pass nếu chưa gọi thành công. Không bật billing hoặc deploy.
```

**Kiểm tra / Nghiệm thu:** fake test pass; live chỉ pass khi thực sự có response hợp lệ, model/usage và dữ liệu an toàn; lỗi/quota → fallback. **Rủi ro/chi phí:** token, egress, secret store và privacy; thiếu key/quyền/ngân sách/cho phép riêng → dừng live, không chặn phần offline.

### Bước 23 — Thẩm định và tích hợp nguồn thứ hai (tùy chọn)

**Mục tiêu/phụ thuộc:** mở rộng thực tế khi giá trị chứng minh được; Bước 4, 7, 11, 12 và policy nguồn. Có thể trước Bước 21 nếu nguồn thứ hai thuộc MVP. **Bạn làm thủ công:** chọn một nguồn và phê duyệt mục đích sử dụng, quota/chi phí, lưu/hiển thị/xóa và quyền thương mại; chỉ tạo credential nếu thật sự cần.

**Prompt gửi AI/Codex:**

```text
Đọc AGENTS.md và tài liệu Giai đoạn 2. Đánh giá một nguồn [người dùng chọn:
Reddit/YouTube/Bluesky/News] qua tài liệu và điều khoản chính thức hiện hành:
quyền thương mại, truy cập API, quota, dữ liệu lưu/tái phân phối/AI, xóa và phí.
Trình bày scope, geography, metric/unit, rate limit và rủi ro; chỉ sau khi được
phép và có bằng chứng quyền phù hợp, triển khai adapter riêng với contract test
fixture, cleaning riêng, provenance và applicability algorithm. Không scraping,
không trộn metric; không gọi nguồn live hoặc dùng key khi chưa được cho phép.
```

**Kiểm tra / Nghiệm thu:** contract/fixture và regression pass; live chỉ nghiệm thu khi có quyền, quota và response thật theo ngân sách được duyệt. **Rủi ro/chi phí:** quyền nguồn có thể cấm tái phân phối/AI/thương mại hoặc thay đổi. Thiếu bằng chứng quyền → `BLOCKED`, không triển khai source thật.

## 10. Các quyết định người dùng cần chốt

| ID | Quyết định cần xác nhận | Mặc định đề xuất, chưa được phê duyệt |
|---|---|---|
| D1 | MVP phục vụ ai, personal/demo hay khách thương mại? | Bắt đầu internal demo; không bán qua Vercel Hobby. |
| D2 | Refresh 7 ngày theo UTC hay múi giờ nào; thời gian, retry, timeout, quota và ai được Start? | 7 ngày UTC; operator duyệt budget; thời điểm và policy chờ chốt. |
| D3 | Retention strict 7 ngày hay gia hạn active khi refresh thất bại? | Gia hạn tối đa 7 ngày, cảnh báo stale, sau đó ẩn/xóa; pháp lý có thể yêu cầu strict. |
| D4 | Thời hạn riêng cho baseline, raw, operational audit và saved research? | Baseline đủ cửa sổ, mục khác chưa chốt; không xóa user data theo dataset. |
| D5 | Taxonomy, mapping `home_goods`, review workflow và ai là reviewer? | Versioned, unclassified khi thiếu evidence; mapping cần duyệt. |
| D6 | Thuật toán cần cho MVP và tiêu chí đạt Precision@K/coverage? | v0.1 + chiến lược phù hợp, báo skipped; ngưỡng xác định sau labeling. |
| D7 | Quyền sử dụng, thương mại, phân phối, AI training/processing và retention cho từng nguồn? | Chỉ Wikimedia theo phạm vi được xác nhận; nguồn khác chờ bằng chứng. |
| D8 | AI tác vụ nào, ngôn ngữ, provider, model, dữ liệu gửi và ngân sách? | AI tắt mặc định; fake để test; không gửi dữ liệu thật khi chưa duyệt. |
| D9 | Platform-managed hay BYOK, ai thanh toán, lưu secret ở đâu? | Platform-managed qua secret manager; BYOK hoãn đến khi có tenant isolation. |
| D10 | Tenant/auth/RBAC, SLO, backup/restore, incident và data residency? | Chưa chốt; bắt buộc trước phát hành thương mại. |
| D11 | Hosting, worker và kế hoạch chuyển Spark/Hobby khi vượt quota hoặc thương mại? | Chưa mua/bật billing; đo usage rồi quyết định. |
| D12 | Policy evidence, IP, trách nhiệm review product ideas và mức market claims? | Chỉ ý tưởng có evidence và review, không suy doanh số từ attention. |

## 11. Kế hoạch kiểm thử và điều kiện hoàn thành

- **Trước mỗi migration:** `cd Backend && .venv/bin/python -m unittest discover -s tests -v`, kiểm tra dependency và `bash -n ../start.sh ../setup.sh`; lưu kết quả thực, không giả định số test cố định.
- **Contract/regression:** golden Wikimedia cũ; schema/API/status; source adapter khác nhau; source nào sai schema/404/429; Unicode, technical namespace, nhiều project/ngày; 0/null/missing/censored; output scoring v0.1 và dashboard cũ.
- **Algorithm/evaluation:** công thức thủ công, missing/MAD=0, cửa sổ/cadence, mixed units, reproducibility, registered strategy SKIPPED/FAILED; labeled data, precision@K/false positive và dữ liệu ngoài mẫu.
- **Storage/jobs:** fake + emulator nếu có; partial writes, stale lock/fencing, concurrent requests, restart, retries, quota, CAS publish, active stale, cleanup subcollections/checkpoint, không chạm users/settings; isolation theo tenant.
- **Security/AI:** authn/authz tenant, no credential leak, prompt injection/invalid schema, model timeout/cost cap, cache partition, BYOK rotation/revoke, fallback và audit. Test provider live chỉ sau phép riêng và budget.
- **UI/E2E:** responsive/keyboard, role access, tìm/lọc/pagination/chi tiết, comparison, states, scheduler Start/Pause/Stop, quality, stale, empty, research unverified; test browser với fake, và một live nhỏ chỉ khi đã duyệt.
- **Operations:** trace request/job có correlation ID, metrics cho quota, chất lượng, stale, job/cleanup/AI; kiểm tra restore trong môi trường thử an toàn; dependency vulnerability/license, CI và rollback, load theo budget, region và chi phí. Không tuyên bố IAM/WIF/deploy/restore live chỉ từ fake.

**Hoàn thành MVP tối thiểu khi:** Bước 1–15 và phần dashboard/auth cần thiết của Bước 20 có bằng chứng nghiệm thu; v0.1 và API demo còn hoạt động; scheduler, lease, publish và retention được test đủ theo policy đã duyệt, và chỉ được bật trên host đã xác nhận; người dùng xem được provenance, chất lượng, tình trạng thiếu và chọn chiến lược phù hợp; đo được budget và có phương án rollback/incident; mọi chức năng ngoài MVP hiển thị disabled/được ghi rõ. Bước 21 ghi từng tiêu chí `PASS/FAIL/BLOCKED` và bằng chứng. Nếu chỉ mới có fake hoặc chưa có host/quyền vận hành, báo **MVP offline**, không gọi là MVP online vận hành.

**Sẵn sàng thương mại là cổng riêng:** hoàn tất tenant isolation và auth đã kiểm chứng, quyền thương mại các nguồn và license, secret management/rotation, retention/deletion/legal, backup/restore, monitoring/on-call, load/cost/SLO, bảo vệ lạm dụng và hosting phù hợp; AI/BYOK/product claims chỉ bật khi có test và review. Spark/Hobby không tự bảo đảm các điều kiện này.

## 12. Tài liệu tham khảo chính thức và kiểm chứng

Đã đối chiếu ngày 09/11/2026; nhà cung cấp có thể thay đổi điều khoản/quota. Kiểm chứng lại trước khi tích hợp hoặc dùng thương mại:

- [Wikimedia Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html) và [access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html): đơn vị/country/top, User-Agent, hạn chế nguồn.
- [Firebase Firestore quotas](https://firebase.google.com/docs/firestore/quotas), [pricing](https://firebase.google.com/docs/firestore/pricing), [delete data](https://firebase.google.com/docs/firestore/manage-data/delete-data), [Firebase billing plans](https://firebase.google.com/docs/projects/billing/firebase-pricing-plans): quota, index, subcollection, TTL/billing; không suy ra quota còn lại từ một báo cáo cũ.
- [Vercel Hobby](https://vercel.com/docs/plans/hobby), [FastAPI](https://vercel.com/docs/frameworks/backend/fastapi), [OIDC Google Cloud](https://vercel.com/docs/oidc/gcp): điều kiện plan, execution và WIF; tài liệu không chứng minh project đã deploy.
- [YouTube quota and compliance](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits), [YouTube developer policies](https://developers.google.com/youtube/terms/developer-policies-guide); [Reddit Data API terms](https://redditinc.com/policies/data-api-terms); [Bluesky API documentation](https://docs.bsky.app/): xác nhận quyền thương mại, data processing và rate cụ thể trước khi dùng. News tùy provider và license từng nguồn.
- [OpenAI platform](https://platform.openai.com/docs/quickstart), [Gemini API key](https://ai.google.dev/gemini-api/docs/api-key): xác nhận models, pricing, data policy và secret trước khi tích hợp thực.
