# TrendScout

Demo khám phá tín hiệu quan tâm qua Wikimedia Pageviews US/all-access. Backend Python 3.12 + FastAPI làm sạch, phân loại và tính điểm heuristic; dashboard hiển thị bằng chứng. Đây không phải xác suất, xu hướng mua sắm hoặc đo lường toàn bộ mạng xã hội.

## Chạy trên Windows

Cần Python native 3.12. `setup.ps1` dùng Python Launcher `py -3.12`, tạo `Backend\.venv` và cài các dependency đã pin trong virtual environment. Có thể cài Python 3.12 qua `winget install --id Python.Python.3.12 --exact` nếu máy chưa có. Nếu PowerShell chặn script cục bộ, thiết lập một lần cho tài khoản hiện tại bằng `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

```powershell
.\setup.ps1
.\start.ps1
```

Mở `http://127.0.0.1:8000/dashboard/`; nhấn Ctrl+C để dừng. `start.ps1` luôn chạy Wikimedia LIVE trên loopback với một worker; không cần API key và không gửi request cho đến khi bấm Fetch. Cấu hình cổng bằng biến môi trường `TREND_SCOUT_DEMO_PORT` (mặc định `8000`). Multi-source Google/Bing LIVE và cloud runtime là các cấu hình riêng, không được `start.ps1` khởi chạy.

Chạy offline regression, browser E2E và benchmark:

```powershell
.\verify_offline_readiness.ps1
```

Script chạy D6.1B security tests trên Windows, toàn bộ unittest, golden tests với hai hash seed, `pip check`, JavaScript syntax, Chrome E2E và storage benchmark. Dùng Chrome cài trên máy; script không tải browser, cài package toàn cục hoặc gọi provider. Windows ACL được đọc và xác minh qua Windows security descriptor; DACL data được giới hạn cho user hiện tại, SYSTEM và Administrators. Quota database, WAL/SHM và process-lock dùng thư mục có ACL riêng dưới `%LOCALAPPDATA%\TrendScout`.

## Kiểm tra offline và nguồn

```powershell
Push-Location Backend
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
    .\.venv\Scripts\python.exe -m pip check
    node --check ..\Frontend\public\app.js
} finally {
    Pop-Location
}
```

Từ thư mục `Backend`, `.\.venv\Scripts\python.exe -m scripts.wikimedia_trend_smoke` gọi Wikimedia thật chỉ đọc, không lưu Firestore; fixture trong test là synthetic. Các dữ liệu chỉ xuất hiện trong top và `views_ceil` xấp xỉ; ngày thiếu, lỗi và trang không lọt top không được chuyển thành 0. Nhóm fashion/home có thể rỗng. Rule phân loại và score chưa được đánh giá như ground truth.

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
