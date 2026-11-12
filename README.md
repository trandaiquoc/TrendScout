# TrendScout

Demo khám phá tín hiệu quan tâm qua Wikimedia Pageviews US/all-access. Backend Python 3.12 + FastAPI làm sạch, phân loại và tính điểm heuristic; dashboard hiển thị bằng chứng. Đây không phải xác suất, xu hướng mua sắm hoặc đo lường toàn bộ mạng xã hội.

## Windows (môi trường phát triển chính)

Cần Python native 3.12, Git, Node.js và Chrome. Giữ nguyên các bản Python khác; `setup.ps1` dùng Python Launcher `py -3.12`, tạo `Backend\.venv` và cài các dependency đã pin trong virtual environment. Có thể cài Python 3.12 qua `winget install --id Python.Python.3.12 --exact` nếu máy chưa có.

```powershell
.\setup.ps1
.\start.ps1 -Synthetic
```

Mở `http://127.0.0.1:8000/dashboard/`; nhấn Ctrl+C để dừng. Demo synthetic dùng fixture cục bộ và không gửi provider request. `start.ps1` không tạo `.env`, API key hoặc credential. Chế độ cloud tiêu chuẩn chỉ chạy khi `GOOGLE_CLOUD_PROJECT` và `TREND_SCOUT_OPERATOR_TOKEN` đã được chủ máy cấu hình tường minh trong environment; ADC cũng phải được thiết lập riêng. Khởi động không gửi provider request. Guarded local LIVE HTTP vẫn cần entrypoint cấp đủ approval/grant backend, `enabled=True`, một worker và các kiểm soát runtime; `start.ps1` không bật chế độ đó.

Chạy offline regression, browser E2E và benchmark:

```powershell
.\verify_offline_readiness.ps1
```

Script chạy D6.1B security tests trên Windows, toàn bộ unittest, golden tests với hai hash seed, `pip check`, JavaScript syntax, Chrome E2E và storage benchmark. Dùng Chrome cài trên máy; script không tải browser, cài package toàn cục hoặc gọi provider. Windows ACL được đọc và xác minh qua Windows security descriptor; DACL data được giới hạn cho user hiện tại, SYSTEM và Administrators. Quota database, WAL/SHM và process-lock dùng thư mục có ACL riêng dưới `%LOCALAPPDATA%\TrendScout`.

## Linux (môi trường kiểm tra phụ)

Giữ `setup.sh`, `start.sh`, `demo.sh` và các kiểm tra seccomp trên Linux. Chạy `bash Backend/scripts/verify_offline_readiness.sh` từ root để kiểm tra Linux security gate, gồm seccomp, syscall và giới hạn tài nguyên. Không mô phỏng kết quả các kiểm tra đó trên Windows. Windows là môi trường phát triển/chạy chính; Linux là kiểm tra tương thích và bảo mật riêng.

## Chạy trên Ubuntu

Cần Python 3.12, gói `python3.12-venv` tương ứng và ADC đã đăng nhập. Việc cài gói hệ thống hoặc đăng nhập ADC được thực hiện riêng; script không tự thay đổi Firebase.

Cài đặt một lần tại root:

```bash
./setup.sh
```

Script tạo `Backend/.venv`, cài dependency và, nếu chưa có cấu hình token, tạo file `.env` local với quyền `600`. Mở `.env` bằng trình soạn thảo trên máy để lấy `TREND_SCOUT_OPERATOR_TOKEN` khi cần chạy phân tích; không gửi token hoặc file này vào chat hay Git. Nếu `.env` đã tồn tại, script không ghi đè. Cũng có thể đặt `GOOGLE_CLOUD_PROJECT` và `TREND_SCOUT_OPERATOR_TOKEN` qua environment; các biến environment được ưu tiên. Token phải có ít nhất 24 ký tự. File `.env` chỉ hỗ trợ giá trị literal `KEY=value` (có thể đặt trong dấu nháy); không thực thi lệnh hoặc nội suy biến shell.

Khởi động toàn bộ backend và dashboard từ root:

```bash
./start.sh
```

Mở [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/). Nhấn `Ctrl+C` tại terminal đang chạy để dừng. Script mặc định cấu hình project `trendscout-192e4` qua environment và sử dụng ADC hiện có; không tự fetch Wikimedia hay tạo run. Nếu chưa có ADC, cấu hình riêng bằng `gcloud auth application-default login`. Khi chủ động chọn **Fetch & Analyze**, nhập token trong dashboard; giữ run ID để mở lại sau restart.

Trước run live, kiểm tra Firebase Console → Firestore → Usage và Project quotas, xác nhận project `trendscout-192e4`, database `(default)` và quota còn lại. Firebase công bố free tier 50.000 reads, 20.000 writes, 20.000 deletes/ngày và 1 GiB storage; số liệu và quota có thể thay đổi. Không tự bật billing. Xem [Firestore quotas](https://firebase.google.com/docs/firestore/quotas) và [theo dõi usage](https://docs.cloud.google.com/firestore/native/docs/monitor-usage).

## Kiểm tra offline và nguồn

```bash
cd Backend
python -m unittest discover -s tests -v
python -m pip check
node --check ../Frontend/public/app.js
```

`python -m scripts.wikimedia_trend_smoke` gọi Wikimedia thật chỉ đọc, không lưu Firestore; fixture trong test là synthetic. Các dữ liệu chỉ xuất hiện trong top và `views_ceil` xấp xỉ; ngày thiếu, lỗi và trang không lọt top không được chuyển thành 0. Nhóm fashion/home có thể rỗng. Rule phân loại và score chưa được đánh giá như ground truth.

## Lỗi thường gặp

- `GET /api/health` thành công nhưng run trả 503: kiểm tra `GOOGLE_CLOUD_PROJECT`, ADC, Firestore và token operator; health không kiểm tra Firestore.
- `401` khi tạo run: kiểm tra token và header `Authorization: Bearer …`; không dán credential vào chat hoặc log.
- `409` hoặc `429` khi tạo run: có run đang hoạt động hoặc đang trong thời gian cooldown.
- `PARTIAL`/`FAILED`/`ABORTED`: mở run để xem lỗi và từng ngày nguồn; không coi `score=null` là 0.
- Monitoring `403` không chứng minh Firestore hết quota; kiểm tra Usage trong Firebase Console. Không bật billing chỉ để kiểm tra.

Chạy demo trên một host; khóa hiện tại không đồng bộ nhiều host. Kết quả live E2E, persistence sau restart và các giới hạn được ghi trong [báo cáo nghiệm thu demo](Documents/Reports/BAO_CAO_NGHIEM_THU_DEMO.md). Khi cần replay, có thể cân nhắc lưu snapshot gốc có provenance, ngày, license và nhãn `REPLAY` trong storage với giới hạn retention; chưa triển khai cơ chế replay ở đây.

## Tài liệu

- [Phân tích nghiệp vụ và thiết kế hệ thống tổng thể](Documents/TrendScout_Phan_Tich_Nghiep_Vu_Thiet_Ke_Tong_The.docx)
- [Thiết kế demo phân tích keyword](Documents/TREND_KEYWORD_DEMO_BUSINESS_SYSTEM_DESIGN.md)
- [Các bước hoàn thành demo](Documents/HUONG_DAN_CAC_BUOC_HOAN_THANH_DEMO.md)
- [Khảo sát dự án tham khảo](Documents/REQUIREMENTS_TECH_BA_DEV.md)
