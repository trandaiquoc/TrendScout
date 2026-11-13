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

## Linux: Wikimedia LIVE local demo

Cần Python 3.12, `python3.12-venv`, và quyền truy cập mạng tới Wikimedia Pageviews. Cài dependencies một lần từ repository root, sau đó chạy:

```bash
bash setup.sh
bash start.sh
```

Mở [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/) và chọn **Fetch new data** để gửi một lượt Fetch giới hạn. Khởi động không gọi nhà cung cấp; Fetch mới gửi yêu cầu khi người dùng bấm nút. Chế độ mặc định dùng guarded normal Wikimedia LIVE, một worker, loopback và local storage. Không cần Firebase, Firestore, ADC, Google Cloud project, operator token hay Wikimedia API key.

`bash setup.sh` cài FastEmbed vào `Backend/.venv`. Bộ sưu tập dùng model multilingual MiniLM chạy CPU cục bộ; model được cache ngoài repository tại `~/.cache/trendscout/fastembed` khi cần. Bộ sưu tập và keyword/evidence snapshot lưu riêng trong `~/.local/state/trendscout/collections-v1/collections.sqlite` và tồn tại sau khi khởi động lại. Nếu model chưa tải hoặc không khả dụng, Wikimedia Fetch vẫn chạy; phân nhóm AI sẽ hiển thị trạng thái không khả dụng. Tab **Collections / Bộ sưu tập** quản lý nhóm, từ khóa, gợi ý và chế độ tạo nhóm. Dữ liệu Fetch còn chịu giới hạn nguồn và lưu trữ; AI chỉ xét toàn bộ record sạch còn khả dụng, không chỉ 100 hàng hiển thị.

`Ctrl+C` dừng tiến trình. Nếu cổng 8000 đang bận, giải phóng cổng hoặc chọn cổng khác bằng `TREND_SCOUT_DEMO_PORT=8123 bash start.sh`. Để chạy fixture ngoại tuyến thay thế, dùng `bash start.sh --synthetic`; dữ liệu đó được gắn nhãn SYNTHETIC và không gọi provider. Các script PowerShell và chế độ Windows không thay đổi.

Chạy `bash Backend/scripts/verify_offline_readiness.sh` để kiểm tra Linux security gate gồm seccomp, syscall và giới hạn tài nguyên; kiểm tra đó không được mô phỏng trên Windows.

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
