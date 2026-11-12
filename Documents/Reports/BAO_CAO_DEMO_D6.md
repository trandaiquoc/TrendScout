# D6 — Personal live demo: báo cáo thực hiện

Ngày cập nhật: 12/11/2026. D6 là tích hợp live local cá nhân; D7 là nghiệm thu end-to-end demo live. Company production và nghiệm thu multi-host thực là track riêng. D5 synthetic demo vẫn độc lập với D6.

## Trạng thái

| Gate | Trạng thái | Bằng chứng và giới hạn |
| --- | --- | --- |
| OFFLINE regression | PASS | `bash Backend/scripts/verify_offline_readiness.sh` trước và sau thay đổi; lần sau gồm 611 unittest, Golden hai hash seed và browser E2E. |
| Cấu hình credentials | PRESENT | Hai key hiện diện trong `.env` local được Git bỏ qua và có quyền 600; hiện diện không chứng minh key hợp lệ hoặc quyền/quota. Không ghi giá trị. |
| LIVE_CONFIGURED | BLOCKED | Gateway và dashboard Fetch live chưa được nối thành runtime local được kiểm thử. |
| LIVE_SMOKE_VERIFIED | BLOCKED | Chưa có phê duyệt smoke/budget; chưa gửi request tới provider. |
| LIVE_DEMO_ACCEPTED | BLOCKED | Chưa có bằng chứng live bốn nguồn. |
| Company production multi-host | BLOCKED, track riêng | Không phải điều kiện chạy demo local. |

Các adapter và gateway D5 đã có, song Public Fetch, response provenance và discovery hiện chủ ý khóa synthetic. D6 cần tích hợp local có kiểm soát và xác minh chính sách trước khi bật live; không thay đổi Vercel read-only. Discovery hỗ trợ đầu vào live có grant backend theo nguồn và giữ nguồn gốc mixed; không tự cấp grant. Thiếu quyền trích xuất thì loại record khỏi discovery; thiếu quyền metric thì giữ `INSUFFICIENT_EVIDENCE`.

Phân loại `topic-terms-v2` bổ sung từ khóa mẫu cho đủ 13 nhóm chủ đề đang hoạt động; đây là heuristic chưa được đánh giá độ chính xác, có thể gán nhiều nhóm hoặc `unclassified`, không chứng minh hiểu hết mọi chủ đề. Định danh keyword và phiên bản thuật toán trích xuất không đổi; Fashion và Home & Living vẫn được ưu tiên nghiên cứu theo quy tắc hiện có.

Giới hạn còn mở trước khi tích hợp discovery live: bộ trích xuất hiện xử lý như tiếng Anh; `hl=en`, `regionCode=US` hoặc vị trí của Wikimedia không xác minh ngôn ngữ của từng nội dung hay nơi cư trú của tác giả/người xem. Cần cơ chế xác định hoặc gắn nhãn ngôn ngữ chưa xác minh và không gọi đó là nhu cầu của toàn bộ thị trường Mỹ.

## Cấu hình local

Chủ dự án tự chỉnh `.env` tại thư mục gốc; file được `.gitignore` loại trừ. `LocalCredentialProvider` chỉ đọc hai tên `YOUTUBE_API_KEY` và `SERPAPI_API_KEY`, không thực thi shell, từ chối symlink, file không thuộc chủ hiện tại, quyền cho group/other hoặc định dạng không hợp lệ. Chạy từ `Backend`: `./.venv/bin/python -m scripts.check_demo_credentials`. Kết quả chỉ là `PRESENT`, `MISSING` hoặc `INVALID_CONFIG`.

## Provider, chi phí và chính sách

Tài liệu chính thức đối chiếu 12/11/2026; mức quota thực tế và điều khoản tài khoản cần chủ tài khoản xác nhận:

- [Wikimedia Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html) yêu cầu User-Agent có thông tin liên hệ và khuyến nghị gửi tuần tự; chưa kiểm chứng khả năng liên hệ của URL cấu hình.
- [SerpApi pricing](https://serpapi.com/pricing) mô tả gói Free 250 searches/tháng và Starter $25/tháng cho 1.000 searches. Search thành công tính quota; kết quả cached/error/failed không tính theo trang này. Gói, credit còn lại, overage và giá thực tế của tài khoản chưa được xác nhận. Google/Bing dùng chung pool.
- [Google](https://serpapi.com/search-api) và [Bing](https://serpapi.com/bing-search-api) SerpApi có thể phục vụ cache dù trạng thái là `Success`; gateway live đặt `no_cache=true` để yêu cầu truy vấn mới, dự trù mỗi request có thể trừ credit. Cache synthetic được ghi rõ; không suy ra thời gian tìm kiếm thực tế chỉ từ lúc backend nhận response.
- [YouTube quota](https://developers.google.com/youtube/data/determine_quota_cost) hiện mô tả `search.list` dùng bucket riêng mặc định 100 calls/ngày, mỗi call 1 đơn vị; trang quota tài khoản mới là nguồn cho quota thực tế. Không tự bật billing.
- [YouTube Developer Policies III.E.2 và III.E.4.h](https://developers.google.com/youtube/terms/developer-policies) giới hạn tổng hợp và tạo dữ liệu/metrics dẫn xuất từ API Data. Áp dụng chính xác với keyword extraction cần xác nhận quyền/chính sách; không tự suy diễn rằng API key cấp quyền đó. [Chính sách lưu trữ](https://developers.google.com/youtube/terms/developer-policies) cũng quy định thời hạn tối đa cho nhiều loại dữ liệu không được ủy quyền. Không cấp grant xử lý live khi chưa xác minh; hiển thị dữ liệu YouTube riêng còn cần kiểm tra yêu cầu branding và chính sách.

Đề xuất smoke tối thiểu để review khi runtime sẵn sàng: tối đa **một request mỗi nguồn**, bốn request tổng, không retry/pagination; trong đó tối đa hai search SerpApi và một YouTube `search.list`. Đây là mức sử dụng quota, không bảo đảm chi phí tiền bằng 0 vì chưa biết gói/overage. Kiểm thử pagination thực cần thêm ngân sách riêng. Chỉ chạy sau khi chủ dự án phê duyệt số request và ngân sách cụ thể.

## Việc còn lại

Xây dựng và kiểm thử runtime local giới hạn peer/host/origin, khóa ngân sách bền vững trên máy, grant ngắn hạn theo nguồn, retention và redaction; bảo toàn disabled trên Vercel. Kiểm tra hiển thị `LIVE`/`CACHED`/`SYNTHETIC`, freshness và lỗi từng nguồn. Xác nhận quyền sử dụng YouTube cho cách xử lý dự kiến. Sau phê duyệt smoke, xác minh từng nguồn độc lập, sau đó tích hợp dashboard và chạy full regression. Không thay mock bằng bằng chứng live.
