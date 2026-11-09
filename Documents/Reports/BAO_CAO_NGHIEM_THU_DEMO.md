# Báo cáo kiểm thử TrendScout — Bước 10

**Thời điểm:** 09/11/2026, UTC+7.
**Project/database:** `trendscout-192e4` / `(default)`, Spark.
**Run thật:** `30e96cf96abe48dbb304fb9808c89873` — **COMPLETED**.
**Kiểm tra khởi động lại:** **PERSISTENCE_VERIFIED**. Không triển khai website hoặc bật billing.

## Trạng thái

| Hạng mục | Kết quả |
|---|---|
| Test offline | COMPLETED — 45 test pass, 0 fail, 0 skip; fixture synthetic không đại diện live |
| Quota | PARTIAL — người vận hành xác nhận Firebase Console: 3 reads, 2 writes, 1 delete trước run. Cloud Monitoring API vẫn trả 403; không hiểu nhầm là Firestore hết quota |
| Dung lượng | PARTIAL — Console không cho biết byte đang dùng. Người vận hành xác nhận chỉ dùng smoke test nhỏ; kiểm tra chỉ đọc không thấy collection cấp cao. Không kết luận dung lượng đã dùng bằng 0 |
| Live E2E | COMPLETED — một POST tạo một run, ghi và đọc Firestore thật |
| Restart | COMPLETED — dừng hẳn backend, khởi động lại, đọc lại metadata, danh sách và cùng chi tiết qua API |
| Dashboard | COMPLETED — run live qua danh sách, tìm kiếm, filter, phân trang và chi tiết; synthetic dùng cho loading, lỗi, replay và trường hợp nhiều trang |
| Nguồn fashion/home | PARTIAL — không có kết quả trong mẫu live; giao diện hiển thị empty state trung thực |
| Nhiều host | NOT_TESTED / chưa hỗ trợ — khóa run chỉ trên cùng host |

## Kiểm soát quota và phạm vi

Firebase Console do người vận hành kiểm tra trước run cho thấy 3/50.000 reads, 2/20.000 writes, 1/20.000 deletes trong khoảng được cung cấp. Dữ liệu hiển thị có độ trễ, không phải số dư chính xác. [Firestore free quota](https://firebase.google.com/docs/firestore/quotas) công bố giới hạn theo ngày và 1 GiB storage. API Cloud Monitoring vẫn `403` yêu cầu billing; không thay đổi gói hoặc IAM.

Ước lượng trước run với mẫu 7.737 observation: 10 snapshot + khoảng 200 observation bucket + 16 keyword shard + dự phòng trạng thái ≈ 236 writes; dự kiến dưới 1.000 reads, không cần deletes. Ngân sách được người vận hành giới hạn 1.000 writes / 5.000 reads; guard của backend từ chối trước ghi bucket nếu dự toán vượt 1.000 writes. Giới hạn document Firestore là 1 MiB; code từ chối JSON trên 700.000 bytes/document để tạo khoảng đệm. Dung lượng JSON của 226 document nội dung lưu thực tế khoảng **9.412.755 bytes**, chưa tính Firestore encoding và index; không suy ra tổng storage hiện có từ con số này.

Run metadata báo `document_writes=226` cho snapshot/bucket/shard; cập nhật trạng thái và tạo run là các writes bổ sung, ước tính khoảng 232 tổng. Lượt kiểm thử dùng 0 deletes. Reads được ước lượng dưới 1.000, không phải số đo billing chính xác; Cloud Monitoring không cung cấp con số xác nhận. Run thành công được giữ lại để xem và kiểm tra persistence.

## Wikimedia → pipeline → Firestore

Lượt live qua backend dùng 10 request Wikimedia; 07–08/11 trả 404 trước khi tìm được ngày phân tích 06/11. Cửa sổ 7 ngày trước ngày phân tích đều tải được. Trước cleaning: 7.999 mục; sau cleaning: **7.737 observation**, ngày phân tích **968 entity**. Firestore ghi 10 snapshot, 200 observation bucket và 16 keyword shard; đọc lại xác nhận 7.737 observation và 968 keyword.

- Có score: **499**; `INSUFFICIENT_DATA`: **469**.
- `EMERGING`: 37; `STABLE`: 308; `FALLING`: 154.
- Bộ lọc ngày phân tích: `all` 968, `fashion` 0, `home_goods` 0, `unclassified` 899; `other` 69. Nhãn chỉ là heuristic title v0.1.
- Đối chiếu bản ghi nguồn trong observation bucket: `en.wikipedia / Stephen_Miller`, ngày 06/11, raw `views_ceil=169000`, `rank=4`; chi tiết đọc lại score 99, baseline 5.200, ratio 32,5 và link `https://en.wikipedia.org/wiki/Stephen_Miller`.

Top 10 theo score (điểm): Stephen Miller 99, Nithya Raman 98, Gina Hinojosa 98, Joe Fagnano 97, Keisha Lance Bottoms 95, Jon Ossoff 85, Pete Hegseth 83, Godzilla Minus Zero 82, Ted Cruz 80, 2026 United States House of Representatives elections 79. So với top 10 theo volume trong nhóm đủ baseline, có 5 mục trùng nhau. Abdul El-Sayed có volume lớn nhất 257.700 nhưng score 47; Rob Sand 70.600, ratio 1,10, STABLE; Hakeem Jeffries ratio 0,61, FALLING; Russell Brand 159.300 nhưng chỉ 2 ngày baseline, `score=null`.

Lượt rà soát có ba mục có vẻ kỹ thuật còn sót: `Wikifunctions:Main Page`, `Wiktionary:Main Page`, `Wikisource:Main Page`. Đây là hạn chế cleaning, chưa khẳng định mọi namespace đã được xử lý. Title `Abdul El-Sayed` hoặc tên người khác có thể thuộc các chủ đề cụ thể nhưng vẫn `unclassified`; dữ liệu lọt top và rule title có thiên lệch, không là ground truth.

## Kiểm thử giao diện và bảo mật

Trình duyệt desktop và mobile đã mở run thật sau restart: 968 mục, trang 1–20 và 21–40; tìm `Stephen Miller`, xem score/baseline/ratio/ngày thiếu, truy cập link evidence; fashion và home rỗng, unclassified 899. Với fixture synthetic 45 mục, đã đi ba trang 20/20/5, tìm kiếm, filter và detail. Các trạng thái giả lập `PARTIAL`, `FAILED`, `ABORTED`, `REPLAY`, loading, lỗi kết nối và thao tác token được kiểm tra tách khỏi live. Token không được lưu trong localStorage hoặc URL; không thực hiện POST live thứ hai.

Test fake/in-memory sau khi tái tạo app không chứng minh Firestore persistence; kết quả `PERSISTENCE_VERIFIED` ở trên dựa vào backend thật đã restart và Firestore thật. Chỉ kiểm tra bảo mật trong phạm vi test; chưa audit đầy đủ WCAG hoặc kiểm thử đa host.

## Chuẩn bị trình diễn và phần còn lại

Có thể trình diễn local bằng run ID ở trên, kèm giải thích dữ liệu cũ và chỉ số thử nghiệm; kiểm tra quota lại trước khi tạo run khác. Hướng dẫn cài đặt, ADC, token và thao tác có trong [README](../../README.md). Đề xuất replay về sau: lưu snapshot nguồn kèm provenance, ngày, điều khoản và chính sách retention, gắn `REPLAY`; chưa xây thêm tính năng. `TS-01` đã xem Usage qua Console, lỗi Monitoring API còn tồn tại; `TS-02` và `TS-03` đã xác minh; `TS-04` vẫn chưa có dữ liệu hai nhóm ưu tiên; `TS-05` chưa hỗ trợ nhiều host; `TS-06` đã kiểm tra phân trang trình duyệt.
