# Hướng dẫn hoàn thành demo TrendScout

**Cập nhật:** 09/11/2026  
**Mục tiêu:** làm demo trong tuần 09–15/11/2026 với **một API thật**, backend, dữ liệu được làm sạch và thuật toán chấm điểm keyword.  
**Cách dùng:** thực hiện lần lượt các bước. Mỗi bước ghi rõ việc **bạn làm thủ công**, **prompt gửi AI** và **kết quả cần kiểm tra**.

Tài liệu này là hướng dẫn triển khai, chưa khẳng định code đã được viết hay Firebase đã được cấu hình. Các lựa chọn công nghệ và công thức là phương án cho demo; tiếp tục tuân thủ `AGENTS.md` tại root và trong thư mục đang làm.

## 1. Phạm vi demo đã điều chỉnh

Demo dùng một API để lấy **danh sách tổng hợp nhiều chủ đề mà API cung cấp**. Hệ thống xử lý toàn bộ các bản ghi hợp lệ trong phạm vi response, sau đó phân loại, xếp hạng và cho phép lọc theo lĩnh vực.

**Thời trang và đồ gia dụng là hai nhóm ưu tiên để xem, kiểm tra và đánh giá kết quả.** Không dùng hai nhóm này làm điều kiện giới hạn dữ liệu đầu vào. Nếu nguồn không có bản ghi phù hợp, giao diện hiển thị không có kết quả và cho biết phạm vi nguồn; không tự tạo trend giả.

```text
Một API thật, nhiều chủ đề
       ↓
Backend fetch → validate → normalize → cleaning → deduplicate
       ↓
Trích keyword / chủ đề → phân loại → đo thay đổi → tính điểm
       ↓
Lưu run, kết quả, lịch sử tối thiểu → Firebase Firestore
       ↓
Dashboard: tất cả chủ đề / lọc thời trang / lọc đồ gia dụng / tìm keyword
```

**Một API không có nghĩa là mọi chủ đề trên Internet.** Demo xử lý tất cả mục hợp lệ trong tập dữ liệu mà endpoint trả về, với giới hạn và độ trễ của nguồn. Kết quả của một nguồn chỉ đại diện cho tín hiệu của nguồn đó.

### Sản phẩm bàn giao của demo

- Backend có API để chạy phân tích thủ công, lấy kết quả và tìm keyword.
- Một adapter API thật, có thể thay hoặc bổ sung adapter khác sau này.
- Pipeline chuẩn hóa, cleaning, chống trùng, trích keyword, phân loại và xếp hạng có thể kiểm thử.
- Firestore lưu trạng thái run và kết quả cần thiết; có thể đọc lại sau khi backend khởi động lại.
- Giao diện mỏng hiển thị **Tất cả**, lọc thời trang/đồ gia dụng, tìm kiếm và xem bằng chứng.
- Có ít nhất một lượt chạy **live đầu-cuối**. Fixture tổng hợp chỉ dùng để test hoặc replay dự phòng và phải gắn nhãn rõ.

Chưa làm trong demo: gợi ý sản phẩm, tích hợp nhiều API cùng lúc, điểm tổng hợp đa nguồn, lịch chạy tự động, cảnh báo và quản lý nhiều vai trò.

## 2. Chọn API thật cho demo

### 2.1. Phương án chính: Wikimedia Pageviews API

Endpoint công khai dưới đây trả danh sách các trang có lượt xem cao theo quốc gia và ngày. Không cần nhập keyword như `fashion`, `home` hay `technology` vào request, nên phù hợp để bắt đầu từ một tập nhiều chủ đề. [Tài liệu Wikimedia Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html).

```text
GET https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/YYYY/MM/DD
```

**Kiểm tra thực tế ngày 09/11/2026 từ môi trường khảo sát:**

| Ngày dữ liệu | Kết quả | Trường quan sát được |
|---|---|---|
| 06/11/2026 | HTTP 200, 1.000 mục | `article`, `project`, `views_ceil`, `rank` |
| 05/11/2026 | HTTP 200, 999 mục | Cùng nhóm trường |
| 08/11/2026 | HTTP 404 | Dữ liệu có thể chưa sẵn sàng; cần thử ngày trước đó |

Đây là **một kiểm tra endpoint**, chưa phải pipeline của repo đã chạy. Ngày mới nhất có thể chưa được provider cập nhật; theo [hướng dẫn xử lý lỗi Wikimedia](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/troubleshooting.html), `404` có thể liên quan dữ liệu chưa được nạp. Hệ thống cần báo ngày dữ liệu thực tế và lùi ngày có giới hạn.

### 2.2. Ý nghĩa và giới hạn của nguồn

- Mỗi mục là một **trang Wikimedia được truy cập**, không phải một bài đăng mạng xã hội, lượng mua hay doanh số.
- Endpoint trả danh sách top có giới hạn; không thể suy ra toàn bộ mọi chủ đề, mọi bài trên Wikipedia hoặc toàn bộ xu hướng mua sắm tại Mỹ.
- `views_ceil` là giá trị dạng cận trên/làm tròn theo cách provider công bố; không hiển thị như số lượt xem chính xác. Không suy số người xem duy nhất từ pageviews.
- Response có thể gồm nhiều project/ngôn ngữ. Lưu `project` cùng `article`; không âm thầm bỏ toàn bộ project khác tiếng Anh. Cho phép lọc ngôn ngữ/project như một chức năng hiển thị nếu cần.
- Danh sách ngày này có thể không có một trang thời trang hoặc đồ gia dụng. Trả “không có kết quả trong dữ liệu nguồn” và không khẳng định lĩnh vực đó không có trend trên thị trường.
- Nếu một trang không xuất hiện trong top của ngày trước, **không coi lượt xem hôm đó bằng 0**. Giá trị lịch sử là không quan sát được trong tập top, có thể do trang không lọt danh sách.
- Tuân thủ yêu cầu nhận diện ứng dụng qua `User-Agent`, truy cập có giới hạn và chính sách nguồn. [Wikimedia access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html).

Theo [Wikimedia về dữ liệu theo quốc gia](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/country-data.html), dữ liệu địa lý có các biện pháp bảo vệ quyền riêng tư. Trước khi triển khai, kiểm chứng cách giải thích `views_ceil` và metadata của endpoint trên response thật.

**Vì sao đổi so với tài liệu demo cũ?** GDELT trong tài liệu trước được đề xuất với truy vấn `technology`, tức đã giới hạn chủ đề. API Wikimedia ở đây cho phép bắt đầu từ một danh sách tổng hợp không cần keyword; GDELT có thể nghiên cứu lại như một adapter về sau. Quyết định này chỉ cho demo, không khẳng định Wikipedia là nguồn phù hợp nhất cho trend thời trang thương mại.

### 2.3. Thời trang/đồ gia dụng được xử lý thế nào?

Sau khi lấy danh sách tổng hợp, hệ thống chạy phân loại để gắn một hoặc nhiều nhãn, chẳng hạn `fashion`, `home_goods`, `other` hoặc `unclassified`. Từ khóa/rule ban đầu chỉ là gợi ý và phải có test; không gán danh mục chắc chắn khi title thiếu ngữ cảnh. Những mục không nhận dạng được vẫn nằm trong **Tất cả**.

Trong demo người dùng có thể lọc `Thời trang` hoặc `Đồ gia dụng` và tìm chuỗi trong danh sách hiện có. Không cam kết endpoint sẽ cung cấp kết quả cho hai nhóm mỗi ngày; nếu mục tiêu bắt buộc là phát hiện sản phẩm viral của hai ngành, cần đánh giá nguồn chuyên ngành khác sau demo.

## 3. Backend và Firebase

| Thành phần | Lựa chọn tạm cho demo | Vai trò |
|---|---|---|
| Backend | Python 3.12 + FastAPI | Fetch, điều phối, xử lý, scoring và API cho giao diện |
| Nguồn | Wikimedia Pageviews API | Lấy dữ liệu thật một nguồn, nhiều chủ đề |
| Phân tích | Python; thư viện text/thống kê khi thực sự cần | Chuẩn hóa, trích keyword và tính tín hiệu |
| Lưu trữ | Firebase Cloud Firestore — Spark | Lưu kết quả, lịch sử tối thiểu và trạng thái run |
| Kiểm thử local | Firestore Emulator hoặc fake repository | Test mà không phụ thuộc cloud mỗi lần |
| Giao diện | HTML/CSS/JS mỏng hoặc công nghệ nhóm chọn | Fetch thủ công, bảng, lọc và chi tiết |
| Backend online nếu cần | Render Free hoặc host phù hợp | Chạy FastAPI; Firestore giữ dữ liệu bền |

**Firestore là nơi lưu dữ liệu; FastAPI là backend chạy thuật toán.** Cloud Functions for Firebase khi deploy cần Blaze. Firestore có free quota trên Spark; tính năng TTL deletes không có free usage và yêu cầu billing, nên demo có thể xóa dữ liệu thủ công theo chính sách hoặc giữ tập nhỏ. Không tự bật billing. [Firebase pricing](https://firebase.google.com/docs/projects/billing/firebase-pricing-plans), [Firestore limits](https://firebase.google.com/docs/firestore/quotas), [Cloud Functions](https://firebase.google.com/docs/functions/get-started).

Theo [Render Free](https://render.com/docs/free), service free có thể ngủ khi không có traffic và filesystem tạm không bền. Không lưu kết quả quan trọng duy nhất ở file local của host. Kiểm tra lại quota và chi phí trước khi tạo dịch vụ.

## 4. Các bước: việc làm tay và prompt cho AI

### Bước 0 — Chuẩn bị repo và quy tắc

**Bạn làm thủ công:**

1. Mở repo `TrendScout` mới trong công cụ làm việc.
2. Kiểm tra `AGENTS.md` ở root và `Frontend/AGENTS.md` đã có trong repo.
3. Xác nhận máy có Python 3.12 hoặc phiên bản tương thích; chưa cần tạo Firebase hay triển khai cloud ở bước này.

**Prompt gửi AI:**

```text
Đọc AGENTS.md ở root và các AGENTS.md có liên quan. Đọc tài liệu
Documents/HUONG_DAN_CAC_BUOC_HOAN_THANH_DEMO.md và phân tích repo hiện tại.
Tóm tắt phạm vi demo, thư mục hiện có, giả định và các điểm còn thiếu.
Chỉ kiểm tra, chưa viết code hoặc triển khai dịch vụ.
```

**Kiểm tra:** AI phân biệt repo mới với dự án cũ; hiểu dữ liệu đầu vào là nhiều chủ đề và thời trang/đồ gia dụng chỉ là nhóm ưu tiên.

### Bước 1 — Thử API thật và xác định hợp đồng nguồn

**Bạn làm thủ công:**

1. Chạy request sau trong terminal. Đây là ngày đã được kiểm tra, không phải cam kết dữ liệu mới nhất:

```bash
curl --fail-with-body --silent --show-error \
  -A 'TrendScoutDemo/0.1 (replace-with-your-contact-or-project-url)' \
  'https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/2026/11/06'
```

2. Thay thông tin liên hệ/project trong `User-Agent` bằng thông tin phù hợp với [chính sách Wikimedia](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html).
3. Thử ngày gần hiện tại và một vài ngày trước; ghi lại HTTP status, ngày, số mục và các trường. Không gửi credential hoặc dữ liệu riêng tư vào chat.

**Prompt gửi AI:**

```text
Kiểm tra endpoint Wikimedia top-per-country US/all-access và response thật
cho ít nhất hai ngày. Không dùng bộ lọc fashion, home hoặc technology ở đầu vào.
Xác minh các trường, giới hạn top, ý nghĩa views_ceil, project, timestamp,
trạng thái 404/429 và User-Agent theo tài liệu chính thức.
Đề xuất canonical schema, khả năng lấy baseline và cách báo dữ liệu chưa có.
Không tạo số liệu hoặc chuyển missing thành 0. Chưa triển khai giao diện.
```

**Kiểm tra:** có ít nhất hai response HTTP 200 ở các ngày khác nhau; ghi rõ số mục, phạm vi top và nguồn. Nếu API trả lỗi, AI báo rõ và thử ngày khác/retry theo chính sách; không tuyên bố live thành công bằng fixture.

### Bước 2 — Khởi tạo backend

**Bạn làm thủ công:**

1. Chuẩn bị môi trường Python trên máy; xem các thay đổi do AI tạo sau khi chạy prompt.
2. Chưa nhập secret hay tạo dịch vụ trả phí.

**Prompt gửi AI:**

```text
Trong Backend/, tạo backend Python + FastAPI tối thiểu cho TrendScout theo
AGENTS.md. Tách API routes, source adapter, canonical models, processing,
analysis, storage interface và orchestration. Tạo health endpoint, cấu hình
qua environment và dependency cần thiết. Không cài frontend hoặc deploy.
Chạy kiểm tra khởi động và báo các lệnh cần dùng.
```

**Kiểm tra:** backend khởi động, `GET /api/health` phản hồi; `health` chỉ chứng minh backend đang chạy. Không có API key hoặc credential trong source.

### Bước 3 — Chuẩn bị Firebase và kết nối lưu trữ

**Bạn làm thủ công:**

1. Mở [Firebase Console](https://console.firebase.google.com/), dùng project có sẵn hoặc tạo project demo do bạn quản lý.
2. Kiểm tra gói **Spark**, tạo Cloud Firestore và chọn cấu hình phù hợp. Không liên kết tài khoản billing nếu chưa quyết định dùng Blaze.
3. Cấu hình quyền/credential theo [Firebase Admin SDK](https://firebase.google.com/docs/admin/setup); sử dụng ADC hoặc cơ chế bí mật của môi trường. Không đưa key vào chat, commit hoặc frontend.
4. Nếu làm local, có thể dùng [Firestore Emulator](https://firebase.google.com/docs/emulator-suite/connect_firestore) trước khi kết nối cloud.

**Prompt gửi AI:**

```text
Thêm storage interface và implementation Cloud Firestore cho backend, cùng
fake hoặc emulator để test. Lưu run, observation, keyword result và bucket
cần thiết; cấu hình qua environment, không hard-code credentials. Đề xuất
Security Rules hạn chế client trực tiếp và kiểm soát quyền ở backend vì
Admin SDK dùng IAM, không bị Security Rules của client chặn.
Kiểm thử ghi/đọc bằng fake hoặc emulator. Không tạo project, bật billing hay deploy.
```

**Kiểm tra:** bạn tự kiểm tra quyền và gói trong Console; test offline qua, và sau khi cấu hình hợp lệ có một lần backend ghi/đọc Firestore thật. Admin SDK phải dùng IAM phù hợp; [server libraries bỏ qua Firestore Security Rules](https://firebase.google.com/docs/firestore/security/get-started).

### Bước 4 — Lấy dữ liệu tổng hợp và chuẩn hóa

**Bạn làm thủ công:**

1. Cung cấp ngày dữ liệu HTTP 200 đã kiểm tra ở bước 1 hoặc cho AI kiểm tra lại endpoint.
2. Xem sample dữ liệu và xác nhận không có truy vấn lọc chủ đề ở đầu vào.

**Prompt gửi AI:**

```text
Triển khai Wikimedia source adapter cho endpoint top-per-country US/all-access.
Thu thập toàn bộ danh sách response cho ngày dữ liệu mới nhất khả dụng và tối đa
7 ngày trước để đối chiếu; retry và lùi ngày có giới hạn, ghi rõ ngày không tải
được. Không lọc đầu vào theo lĩnh vực hoặc ngôn ngữ.

Chuẩn hóa tối thiểu source, country_scope, project, article_original,
canonical_keyword, views_ceil, rank, observation_date, fetched_at, source_url,
quality_flags. Dùng khóa (project, article) để tránh gộp nhầm giữa các project.
Bảo toàn bản gốc, nguồn và phạm vi top. Thêm contract test từ fixture tổng hợp.
```

**Kiểm tra:** số bản ghi đọc được khớp response trước khi làm sạch; danh sách có nhiều loại chủ đề; `project`, ngày và `views_ceil` được giữ. Hệ thống báo rõ `404`, `429`, lỗi parse và lịch sử thiếu.

### Bước 5 — Cleaning và chống trùng

**Bạn làm thủ công:**

1. Xem báo cáo mẫu trước/sau cleaning và các bản ghi bị loại.
2. Xác nhận các mục kỹ thuật như `Main_Page` hoặc namespace đặc biệt được ghi nhận lý do loại; không loại các mục bình thường chỉ vì không thuộc hai ngành ưu tiên.

**Prompt gửi AI:**

```text
Thêm validate, Unicode normalization, giải mã title đúng cách, chuẩn hóa
khoảng trắng và cleaning cho dữ liệu Wikimedia. Giữ title gốc để đối chiếu.
Loại các mục kỹ thuật theo danh sách rõ ràng; deduplicate theo project + article,
không gộp nhầm bài trùng tên ở hai project. Thống kê accepted, rejected,
duplicates và lý do. Phân biệt views_ceil = 0, missing và ngày không lấy được.
Viết test cho Unicode, namespace, trùng lặp, bad timestamp và missing.
```

**Kiểm tra:** các mục hợp lệ thuộc chủ đề khác vẫn được giữ; báo cáo giải thích được số bị loại/trùng. Không đưa dữ liệu thiếu thành số 0.

### Bước 6 — Trích xuất keyword và phân loại chủ đề

**Bạn làm thủ công:**

1. Chọn một số ví dụ thật trong response để đối chiếu, gồm mục thời trang/đồ gia dụng nếu có và mục khác.
2. Xác nhận giao diện vẫn có nhóm **Tất cả** và các mục chưa phân loại.

**Prompt gửi AI:**

```text
Từ article title đã làm sạch, tạo keyword/chủ đề có thể tìm kiếm; giữ
canonical entity đầy đủ thay vì chỉ dựa vào các từ đơn. Có thể dùng ngram
hoặc TF-IDF để hỗ trợ nhưng không biến điểm TF-IDF thành điểm trend.
Tạo phân loại ban đầu cho fashion và home_goods bằng rule/từ điển có version;
hỗ trợ other/unclassified và một mục có thể có nhiều nhãn. Không giới hạn
kết quả tổng thể vào hai nhóm này, không gán nhãn chắc chắn khi thiếu bằng chứng.
Viết test cho ít nhất 2 nhóm ưu tiên, mục khác và mục chưa phân loại.
```

**Kiểm tra:** có danh sách toàn bộ keyword hợp lệ; lọc hai nhóm chỉ ảnh hưởng kết quả hiển thị, không ảnh hưởng thu thập. Nếu không có mục của nhóm ưu tiên, báo không có dữ liệu phù hợp.

### Bước 7 — Xây thuật toán tính điểm

**Bạn làm thủ công:**

1. Đọc cách tính dưới đây và xem ví dụ đầu vào/đầu ra do AI báo cáo.
2. Đánh giá vài kết quả thật: có thể mở bằng chứng, hiểu ngày so sánh và trạng thái dữ liệu hay không.

**Quy tắc khởi đầu cho nguồn Wikimedia:**

- Current là `views_ceil` của trang trong ngày phân tích; baseline là median của tối đa 7 ngày trước **mà trang thực sự xuất hiện trong danh sách top**.
- Cần ít nhất 3 ngày baseline có quan sát hợp lệ để tính điểm; dưới mức đó trả `score=null`, `INSUFFICIENT_DATA`. Ghi số ngày có dữ liệu và các ngày không quan sát được.
- Vắng mặt trong danh sách top là **censored/không quan sát được**, không phải 0. Median tính trên các ngày quan sát được có thiên lệch; phải hiển thị cảnh báo độ phủ.
- Với current > 0 và baseline > 0, có thể thử `G = clip(log2(current / baseline) / 3, 0, 1)`; `V = log1p(current) / log1p(max_views_ceil_trong_ngày)`; `score = round(100 × (0.7G + 0.3V))`.
- Điểm là heuristic thử nghiệm dựa trên cận trên pageviews; không phải xác suất, lượt mua, hay trend social. Trường hợp baseline 0 hoặc giá trị không hợp lệ trả `INSUFFICIENT_DATA` thay vì chia 0.
- Nhãn đề xuất: `EMERGING` nếu ratio ≥ 1,5 và score ≥ 50; còn lại `STABLE` hoặc `FALLING` nếu ratio < 0,75. Ngưỡng cần hiệu chỉnh sau khi xem dữ liệu thật; không gọi nhãn là ground truth.

**Prompt gửi AI:**

```text
Triển khai scoring v0.1 đúng quy tắc Wikimedia ở mục 4 bước 7 của tài liệu.
Gộp theo project + article, dùng current và baseline median của tối đa 7 ngày
trước có quan sát; cần tối thiểu 3 ngày. Không coi ngày vắng khỏi top là 0;
không thay baseline thiếu, 0 hoặc API lỗi bằng số giả.
Trả score hoặc null, status, observed_baseline_days, baseline,
current_views_ceil, ratio, score_components, algorithm_version,
quality_flags và evidence link. Ghi nhãn độ phủ/censored.
Viết test tăng, ổn định, giảm, thiếu ngày, baseline 0, ngày lỗi, project trùng
tên và tính tái lập. So sánh với baseline xếp chỉ theo views_ceil.
```

**Kiểm tra:** keyword tăng rõ được nhận diện trong fixture; trường hợp không đủ 3 ngày hoặc API lỗi không bị gắn điểm chắc chắn. Kết quả thật ghi rõ giá trị làm tròn và chỉ trong top của nguồn.

### Bước 8 — Lưu kết quả và cung cấp API backend

**Bạn làm thủ công:**

1. Kiểm tra project Firebase và cấu hình môi trường; không dán credential vào prompt.
2. Chạy thử backend theo hướng dẫn AI cung cấp, kiểm tra một run được lưu/đọc lại.

**Prompt gửi AI:**

```text
Thêm API cho run thủ công và đọc kết quả: POST /api/runs,
GET /api/runs/{run_id}, GET /api/runs/{run_id}/keywords?q=... và
GET /api/runs/{run_id}/keywords/{keyword_id}. Tìm kiếm mặc định chỉ tìm trong
kết quả đã có; hỗ trợ filter all, fashion, home_goods, unclassified.
Lưu run metadata, observations, buckets và keyword results trong Firestore
qua storage interface. Ghi source, data_date, fetched_at, mode, status,
algorithm_version, coverage và lỗi. Tránh fetch trùng khi bấm liên tục;
run bị gián đoạn phải có trạng thái rõ ràng. Không deploy.
```

**Kiểm tra:** có thể fetch → nhận `run_id` → xem trạng thái → đọc danh sách/chi tiết; restart backend vẫn đọc được kết quả từ Firestore thật. Không có endpoint public không giới hạn để người lạ kích hoạt fetch.

### Bước 9 — Làm giao diện demo

**Bạn làm thủ công:**

1. Mở giao diện trên máy, tự bấm fetch, tìm kiếm và lọc để đánh giá.
2. Kiểm tra ngày dữ liệu, nguồn, cảnh báo và link bằng chứng hiển thị dễ hiểu.

**Prompt gửi AI:**

```text
Đọc Frontend/AGENTS.md và tạo giao diện demo mỏng kết nối backend hiện có.
Có nút Fetch & Analyze; bảng keyword với score, status, project, ngày dữ liệu,
nguồn và cảnh báo; tìm kiếm local; bộ lọc Tất cả, Thời trang, Đồ gia dụng,
Chưa phân loại; xem chi tiết baseline, views_ceil, số ngày quan sát và link gốc.
Không gọi upstream theo từng ký tự, không đặt secret trong frontend.
Thể hiện loading, empty, partial, error, stale và replay. Nếu nhóm fashion/home
không có dữ liệu, hiển thị empty state thay vì tạo dữ liệu.
Chạy build/test hiện có và kiểm tra giao diện bằng trình duyệt nếu khả dụng.
```

**Kiểm tra:** bộ lọc **Tất cả** hiển thị các chủ đề khác ngoài thời trang/đồ gia dụng; có thể tìm kiếm, xem chi tiết và hiểu dữ liệu bị giới hạn bởi endpoint.

### Bước 10 — Test đầu-cuối và chuẩn bị demo

**Bạn làm thủ công:**

1. Mở backend và giao diện; dùng một run live, chọn **Tất cả**, sau đó thử hai bộ lọc ưu tiên và tìm keyword.
2. Đối chiếu ít nhất một kết quả với trang nguồn và ngày dữ liệu được ghi nhận.
3. Quyết định có cần link online không. Nếu chỉ demo trên máy, chưa cần deploy. Nếu cần online, chuẩn bị tài khoản host và kiểm tra gói/quota trước khi triển khai.

**Prompt gửi AI:**

```text
Kiểm thử end-to-end demo TrendScout theo AGENTS.md và tài liệu hướng dẫn:
API thật -> adapter -> normalize/clean/dedup -> keyword/category -> score ->
Firestore -> backend -> UI. Chạy test offline có fixture tổng hợp và một live
smoke riêng; kiểm tra response nhiều chủ đề, all/fashion/home_goods,
missing lịch sử, 404/429, stale, restart và secret không bị commit.
Báo kết quả thật, phạm vi dữ liệu, ngày, số record, hạn chế và các lỗi còn lại.
Không giả lập live, không bật billing và không deploy nếu chưa được yêu cầu.
```

**Kiểm tra:** một run live hoàn thành đầu-cuối; kết quả có nhiều chủ đề, nguồn và ngày; hai nhóm ưu tiên lọc được hoặc báo empty; dữ liệu đọc lại được; điểm giải thích được; test và hạn chế được báo rõ.

## 5. Thêm API khác sau demo

Thiết kế adapter giúp tái sử dụng cleaning, schema, scoring và giao diện, nhưng mỗi nguồn có khả năng, đơn vị, phạm vi và điều khoản khác nhau.

1. Kiểm tra API mới có thật, quyền truy cập, quota và cách lấy dữ liệu nhiều chủ đề.
2. Viết adapter map dữ liệu vào schema chung và khai báo capabilities: text, geography, timestamp, lịch sử và loại số đo.
3. Viết contract test cho response, lỗi, missing và timestamp. Giữ provenance của nguồn mới.
4. Đánh giá scorer: nếu nguồn không có baseline hoặc đơn vị tương thích, dùng scoring profile riêng hoặc trả `INSUFFICIENT_DATA`; không chế số liệu để dùng chung công thức.
5. Cho phép xem/lọc từng nguồn riêng. Chỉ tổng hợp điểm đa nguồn khi đã thiết kế và kiểm chứng cách chuẩn hóa; không cộng thẳng views, số bài và tương tác.

Không cần viết lại toàn bộ pipeline khi thêm nguồn, nhưng có thể cần **bổ sung mapping, capability và thuật toán riêng** để phản ánh khác biệt của API mới.

## 6. Checklist nghiệm thu demo

- [ ] Một API thật được gọi thành công; dữ liệu live có nhiều chủ đề trong phạm vi endpoint.
- [ ] Không có điều kiện `fashion`, `home` hoặc một ngành duy nhất trong request khám phá ban đầu.
- [ ] Có báo cáo validate/cleaning/dedup và giữ `project + article`, ngày, nguồn và `views_ceil`.
- [ ] Có kết quả **Tất cả**; lọc thời trang/đồ gia dụng và nhóm chưa phân loại hoạt động hoặc báo empty trung thực.
- [ ] Có baseline hợp lệ, công thức/version và mức đủ dữ liệu; thiếu ngày không bị đổi thành 0.
- [ ] Backend có API fetch, trạng thái, danh sách, tìm kiếm và chi tiết; không lộ secret.
- [ ] Firestore thật lưu/đọc lại được một run; dữ liệu lỗi hoặc replay được gắn nhãn.
- [ ] Có test offline, live smoke, README chạy local và báo cáo hạn chế nguồn.

## 7. Lịch gợi ý trong tuần

| Ngày | Việc chính | Đầu ra |
|---|---|---|
| 1 | Bước 0–2: đọc rule, thử API thật, khởi tạo backend | Response tổng hợp thật và backend chạy được |
| 2 | Bước 3–4: Firebase, adapter và schema | Ghi/đọc run và lấy được dữ liệu chuẩn |
| 3 | Bước 5–7: cleaning, keyword, phân loại và scoring | Pipeline có test và giải thích điểm |
| 4 | Bước 8–9: API kết quả, dashboard và filter | Luồng thao tác đầu-cuối trên máy |
| 5 | Bước 10: kiểm tra live, sửa lỗi, chuẩn bị demo | Một run live + replay dự phòng + README |

Ngày 13/11/2026 có thể dùng làm mốc đệm; hoàn thành trong tuần 09–15/11/2026 theo thời gian thực tế. Nếu trễ, giảm độ phức tạp giao diện hoặc số ngày test, nhưng không bỏ nguồn thật, tính đúng missing, backend và bằng chứng.

## 8. Tài liệu liên quan

- [Phân tích nghiệp vụ và thiết kế tổng thể](TrendScout_Phan_Tich_Nghiep_Vu_Thiet_Ke_Tong_The.docx)
- [Thiết kế demo trước đó — tham khảo, phần GDELT và công thức chỉ áp dụng nếu chọn lại nguồn đó](TREND_KEYWORD_DEMO_BUSINESS_SYSTEM_DESIGN.md)
- [Wikimedia Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/)
- [Wikimedia access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html)
- [Firestore limits](https://firebase.google.com/docs/firestore/quotas)
- [Firebase Admin SDK](https://firebase.google.com/docs/admin/setup)
- [Firebase Cloud Functions](https://firebase.google.com/docs/functions/get-started)
- [Render Free](https://render.com/docs/free)

Nguồn ngoài được đối chiếu ngày 09/11/2026. Chưa tạo Firebase project, triển khai backend hay kích hoạt dịch vụ trả phí trong quá trình sửa tài liệu.
