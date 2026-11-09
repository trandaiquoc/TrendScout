# TrendScout Dashboard

Giao diện HTML/CSS/JavaScript thuần, được FastAPI phục vụ cùng origin tại `/dashboard/`. Không cần cài npm hoặc build frontend; trình duyệt không gọi trực tiếp Wikimedia hay Firestore.

## Chạy trên Ubuntu

Từ repository:

```bash
cd Backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export GOOGLE_CLOUD_PROJECT="trendscout-192e4"
# ADC phải được cấu hình trên máy; không đưa credential vào repository.
export TREND_SCOUT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở `http://127.0.0.1:8000/dashboard/`. Nhập run ID đã có để xem dữ liệu đã lưu, hoặc chọn **Fetch & Analyze** và nhập operator token trong hộp thoại. Token chỉ được giữ trong bộ nhớ khi gửi request, không được lưu vào localStorage, URL hay source. Server và giao diện nên chạy trên localhost; xem [Backend README](../Backend/README.md) trước khi cho phép truy cập mạng. Không tự khởi chạy phân tích khi reload.

Backend hiện không có API liệt kê run; cần giữ run ID để mở lại. Dashboard dùng các API `/api/health`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/keywords` và chi tiết keyword. Tìm kiếm có debounce, chỉ đọc kết quả backend đã lưu. Phân trang theo API; nhóm không có kết quả hiển thị `NO_MATCHES_IN_ANALYZED_DATA`.

Điểm số là heuristic của backend, không phải xác suất hoặc bằng chứng nhu cầu mua. Giao diện phân biệt giá trị thiếu với 0, nguồn và ngày quan sát; thời gian fetch hiển thị theo múi giờ của máy. Nhãn `SYNTHETIC`/`REPLAY` dựa trên `mode` của run. Dữ liệu cũ hơn 3 ngày theo đồng hồ máy được cảnh báo; đây là quy ước hiển thị, không phải cam kết độ trễ nguồn.

## Kiểm tra

```bash
cd Backend
python -m unittest discover -s tests -v
node --check ../Frontend/public/app.js
```

Test backend dùng fake repository; fixture UI được gắn nhãn `SYNTHETIC`. Kiểm thử trình duyệt với fake không thay thế nghiệm thu live Firestore. Không tự tạo run live khi chưa xác minh quota. Xem [Backend README](../Backend/README.md) về token, storage, trạng thái run và giới hạn chạy trên một host.
