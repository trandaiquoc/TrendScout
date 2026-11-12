# D6 — Personal live demo: báo cáo thực hiện

Ngày cập nhật: 12/11/2026. D6 là tích hợp live local cá nhân; D7 là nghiệm thu end-to-end demo live. Company production và nghiệm thu multi-host thực là track riêng. D5 synthetic demo vẫn độc lập với D6.

## Trạng thái

| Gate | Trạng thái | Bằng chứng và giới hạn |
| --- | --- | --- |
| OFFLINE regression | PASS | `bash Backend/scripts/verify_offline_readiness.sh` gần nhất gồm 634 unittest, Golden hai hash seed và browser E2E; không chứng minh provider live. |
| Cấu hình credentials | PRESENT | Hai key hiện diện trong `.env` local được Git bỏ qua và có quyền 600; hiện diện không chứng minh key hợp lệ hoặc quyền/quota. Không ghi giá trị. |
| LIVE_CONFIGURED | BLOCKED | Gateway và dashboard Fetch live chưa được nối thành runtime local được kiểm thử. |
| LIVE_SMOKE_VERIFIED | BLOCKED | Chủ dự án đã phê duyệt tối đa bốn request, không retry/phân trang, ngân sách phát sinh 0 đồng và xác nhận quota SerpApi; runtime chưa sẵn sàng và yêu cầu sau đó cấm gọi YouTube live hoặc dùng key thật. Chưa gửi request tới provider. |
| LIVE_DEMO_ACCEPTED | BLOCKED | Chưa có bằng chứng live bốn nguồn. |
| Company production multi-host | BLOCKED, track riêng | Không phải điều kiện chạy demo local. |

Các adapter và gateway D5 đã có, song Public Fetch, response provenance và discovery hiện chủ ý khóa synthetic. D6 cần tích hợp local có kiểm soát và xác minh chính sách trước khi bật live; không thay đổi Vercel read-only. Discovery hỗ trợ đầu vào live có grant backend theo nguồn và giữ nguồn gốc mixed; không tự cấp grant. Thiếu quyền trích xuất thì loại record khỏi discovery; thiếu quyền metric thì giữ `INSUFFICIENT_EVIDENCE`.

Đã chuẩn bị `LocalLiveGuard` và test chặn peer/Host ngoài loopback, proxy headers và cross-origin; guard chưa được gắn vào live app. Đây không phải xác thực và không chặn local direct clients; bắt buộc có quota/cooldown, giới hạn đồng thời và phê duyệt riêng trước khi kết nối provider.

Đã chuẩn bị policy smoke mặc định tắt với một lượt Wikimedia, hai lượt chung SerpApi, một lượt YouTube theo ngày/tháng, một job mỗi 24 giờ và mỗi pool tối đa một attempt đồng thời. `LocalSmokeStore` giới hạn một reservation cho từng nguồn trong ledger, giữ bộ đếm qua khởi tạo lại và không cho Google dùng lượt của Bing; test dùng SQLite local. Cost units nội bộ chỉ đếm attempt, không phải tiền hoặc quota còn lại của provider. Policy và wrapper chưa được gắn vào runtime; ledger thật còn cần được bố trí tại đường dẫn owner-only bền vững trước smoke.

SQLite có factory riêng cho dữ liệu live local, yêu cầu thư mục owner-only và từ chối Vercel; factory không bật API. Cần thiết kế và kiểm chứng xóa dữ liệu, kể cả WAL sau crash, trước khi lưu dữ liệu provider thật. Ledger quota cần tồn tại qua restart; không coi xóa ledger là cách cấp thêm ngân sách.

`LocalVolatileCheckpoints` giữ checkpoint nguồn ở bộ nhớ tiến trình, còn quota receipt lưu trong SQLite; lock ngăn hai tiến trình cùng dùng lớp này và dữ liệu checkpoint mất khi restart. Facade live giờ yêu cầu lớp này thay vì nhận SQLite trực tiếp, và gateway chấp nhận lớp bọc tương ứng. Kiểm thử offline kiểm tra facade từ chối SQLite trực tiếp và dữ liệu fixture không được ghi vào file ledger. Facade vẫn chưa được nối vào live runtime; kiểm thử không chứng minh xử lý crash, swap, core dump hoặc retention của dữ liệu provider thật.

Kiểm thử ghép offline `test_d6_offline_worker_composition` chạy worker với bốn gateway `MockTransport`, quota smoke, checkpoint tạm và query plan cố định: mỗi nguồn có một request fixture, duplicate cùng job, và nội dung fixture kiểm tra không xuất hiện trong ledger. Các kết quả được gắn `SYNTHETIC`; đây không phải bằng chứng live hoặc bằng chứng chính sách provider đã được chấp thuận.

Gateway phân biệt scope `LOCAL_SMOKE` với `PRODUCTION`: local bắt buộc policy smoke cố định, wrapper giới hạn nguồn và SQLite live local, cùng các grant server-side khác; production tiếp tục đòi bằng chứng distributed controls. Tạo scope này không tự cấp phê duyệt provider và không chứng minh an toàn production.

`scripts.d6_local_smoke` là công cụ smoke đề xuất, mặc định từ chối chạy; test mock kiểm tra bốn lần gọi tối đa và không lưu body response vào ledger. Chỉ dùng sau khi chủ dự án phê duyệt bốn request, xác nhận quota, không phát sinh phí và quyền sử dụng; không suy ra approval từ việc có key. Công cụ chưa được chạy live và chưa thay thế kiểm tra pagination hoặc D7 E2E. Mỗi ledger chỉ chấp nhận một attempt mỗi nguồn; không xóa ledger để vượt giới hạn.

Dashboard đã chuẩn bị để nhận `LIVE`/`MIXED` chỉ khi capability xác định rõ demo local và không read-only; nguồn đang hoạt động được ghi `Live local`, dữ liệu thiếu mode không bị tự gắn `SYNTHETIC`. Kiểm thử gate UI là offline, chưa chứng minh live UI hoặc YouTube branding compliance.

Fetch facade hỗ trợ contract live local chỉ khi dùng policy smoke và ledger local tương ứng; live result chỉ trả tóm tắt từng nguồn, không trả records thô. Discovery vẫn cần processing grant theo nguồn. Đây mới là kiểm thử contract với dữ liệu giả lập, chưa nối vào API/dashboard hay gọi nguồn thật.

API capability và response tiếp nhận Fetch lấy data mode từ facade được cấu hình, thay vì luôn gắn `SYNTHETIC`; chế độ live vẫn bị chặn ở application factory. Khi checkpoint không còn, status không suy ra `LIVE` chỉ từ retention grant. Kiểm thử offline xác nhận đường synthetic và trường hợp kết quả volatile bị mất; chưa có bằng chứng HTTP live.

Khi gửi lại idempotency key, admission trả trạng thái job thực tế trong cùng transaction; API không khởi động lại background worker cho job đã kết thúc. Kiểm thử API synthetic xác nhận replay sau khi hoàn tất báo cùng trạng thái với endpoint status. Điều này không thay thế kiểm thử live hoặc supervisor sau crash.

`local_live_composition` ghép facade, worker, bốn gateway, query plan cố định, quota và checkpoint tạm thành một context backend; constructor yêu cầu các approval/grant do backend cấp, không tự đọc key hoặc gửi request. Context đóng gateway và giải phóng lock khi thoát; Vercel bị từ chối. Kiểm thử offline xác minh thiếu approval bị chặn và không có request trong lúc khởi tạo. Smoke CLI và composition dùng cùng đường dẫn và namespace ledger cố định; kiểm thử cho thấy hai phía cùng thấy job và reservation. Đây chỉ là ranh giới trong mã local: người có quyền máy có thể sửa code hoặc xóa ledger, nên cần kiểm soát vận hành; không coi đó là chống lạm dụng production. Chưa có HTTP runtime, policy consent hay kiểm chứng provider; không dùng context này để tuyên bố live ready.

Composition giới hạn thời hạn logic của checkpoint grant tối đa 120 giây và giữ nguyên grant ngắn hơn hoặc thời điểm hết hạn sớm hơn; grant bị từ chối, sai nguồn hoặc sai mode không được biến thành quyền hợp lệ. Bộ quét chạy định kỳ, do đó đây không phải chứng minh xóa vật lý chính xác sau 120 giây hoặc xóa mọi bản sao trong bộ nhớ, swap, log và crash dump. Chính sách công khai vẫn chờ chủ dự án duyệt và kiểm chứng toàn bộ đường đi dữ liệu.

Phân loại `topic-terms-v2` bổ sung từ khóa mẫu cho đủ 13 nhóm chủ đề đang hoạt động; đây là heuristic chưa được đánh giá độ chính xác, có thể gán nhiều nhóm hoặc `unclassified`, không chứng minh hiểu hết mọi chủ đề. Định danh keyword và phiên bản thuật toán trích xuất không đổi; Fashion và Home & Living vẫn được ưu tiên nghiên cứu theo quy tắc hiện có.

Bộ trích xuất vẫn dùng heuristic hướng tiếng Anh; bản ghi LIVE đi vào discovery hiện được gắn ngôn ngữ `und` (chưa xác minh) và keyword có cờ `LANGUAGE_UNVERIFIED`. Bộ lọc `hl=en`, `regionCode=US` hoặc vị trí của Wikimedia không xác minh ngôn ngữ từng nội dung hay nơi cư trú của tác giả/người xem. Nhãn này không phát hiện ngôn ngữ và không chứng minh độ chính xác của heuristic trên nội dung đa ngôn ngữ; không gọi kết quả là nhu cầu của toàn bộ thị trường Mỹ.

## Cấu hình local

Chủ dự án tự chỉnh `.env` tại thư mục gốc; file được `.gitignore` loại trừ. `LocalCredentialProvider` chỉ đọc hai tên `YOUTUBE_API_KEY` và `SERPAPI_API_KEY`, không thực thi shell, từ chối symlink, file không thuộc chủ hiện tại, quyền cho group/other hoặc định dạng không hợp lệ. Chạy từ `Backend`: `./.venv/bin/python -m scripts.check_demo_credentials`. Kết quả chỉ là `PRESENT`, `MISSING` hoặc `INVALID_CONFIG`.

## Provider, chi phí và chính sách

Tài liệu chính thức đối chiếu 12/11/2026; mức quota thực tế và điều khoản tài khoản cần chủ tài khoản xác nhận:

- [Wikimedia Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html) yêu cầu User-Agent có thông tin liên hệ và khuyến nghị gửi tuần tự; chưa kiểm chứng khả năng liên hệ của URL cấu hình.
- [SerpApi pricing](https://serpapi.com/pricing) mô tả gói Free 250 searches/tháng và Starter $25/tháng cho 1.000 searches. Search thành công tính quota; kết quả cached/error/failed không tính theo trang này. Gói, credit còn lại, overage và giá thực tế của tài khoản chưa được xác nhận. Google/Bing dùng chung pool.
- Điều khoản sử dụng và quyền xử lý dữ liệu SerpApi cho trường hợp demo này chưa được xác nhận; trang điều khoản không truy cập được trong lần kiểm tra, nên không xem quyền đó là đã được cấp.
- [Google](https://serpapi.com/search-api) và [Bing](https://serpapi.com/bing-search-api) SerpApi có thể phục vụ cache dù trạng thái là `Success`; gateway live đặt `no_cache=true` để yêu cầu truy vấn mới, dự trù mỗi request có thể trừ credit. Cache synthetic được ghi rõ; không suy ra thời gian tìm kiếm thực tế chỉ từ lúc backend nhận response.
- [YouTube quota](https://developers.google.com/youtube/data/determine_quota_cost) hiện mô tả `search.list` dùng bucket riêng mặc định 100 calls/ngày, mỗi call 1 đơn vị; trang quota tài khoản mới là nguồn cho quota thực tế. Không tự bật billing.
- [YouTube Developer Policies III.E.2 và III.E.4.h](https://developers.google.com/youtube/terms/developer-policies) giới hạn tổng hợp và tạo dữ liệu/metrics dẫn xuất từ API Data. Áp dụng chính xác với keyword extraction cần xác nhận quyền/chính sách; không tự suy diễn rằng API key cấp quyền đó. [Chính sách lưu trữ](https://developers.google.com/youtube/terms/developer-policies) cũng quy định thời hạn tối đa cho nhiều loại dữ liệu không được ủy quyền. Không cấp grant xử lý live khi chưa xác minh; hiển thị dữ liệu YouTube riêng còn cần kiểm tra yêu cầu branding và chính sách.

Chủ dự án đã phê duyệt smoke tối đa **một request mỗi nguồn**, bốn request tổng, không retry/pagination; tối đa hai search SerpApi và một YouTube `search.list`, ngân sách phát sinh 0 đồng, và xác nhận quota SerpApi không tính phí vượt gói. Phê duyệt này không xác nhận quyền xử lý YouTube, không mở rộng sang pagination và không vượt qua yêu cầu sau đó của chủ dự án cấm gọi YouTube live hoặc dùng API key thật. Smoke chưa chạy; cần runtime và quyền provider phù hợp trước khi thực hiện các phần được phép.

## Việc còn lại

Xây dựng và kiểm thử runtime local giới hạn peer/host/origin, khóa ngân sách bền vững trên máy, grant ngắn hạn theo nguồn, retention và redaction; bảo toàn disabled trên Vercel. Kiểm tra hiển thị `LIVE`/`CACHED`/`SYNTHETIC`, freshness và lỗi từng nguồn. Xác nhận quyền sử dụng YouTube cho cách xử lý dự kiến. Khi các điều kiện và chỉ dẫn hiện hành cho phép, xác minh từng nguồn độc lập, tích hợp dashboard và chạy full regression. Không thay mock bằng bằng chứng live.
