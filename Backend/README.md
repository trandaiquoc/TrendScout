# TrendScout backend — Bước 2

Khung Python 3.12 + FastAPI. Hiện chỉ có endpoint kiểm tra tiến trình; chưa kết nối Wikimedia hoặc Firestore và chưa chạy phân tích.

## Thiết lập trên Ubuntu

Cần Python 3.12 và gói venv tương ứng. Nếu `python3.12 -m venv` báo thiếu `ensurepip`, cài `python3.12-venv` từ kho Ubuntu đã cấu hình (ví dụ `sudo apt install python3.12-venv`). Nếu bản Ubuntu không có sẵn Python 3.12, trước tiên cài Python 3.12 và gói venv tương thích từ nguồn phù hợp với bản Ubuntu.

Từ thư mục `Backend/`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Ở terminal khác:

```bash
curl -i http://127.0.0.1:8000/api/health
```

Kết quả dự kiến HTTP 200 và `{"status":"ok"}`. Health chỉ phản ánh backend phản hồi; không kiểm tra nguồn dữ liệu hoặc storage.

## Cấu hình

`TREND_SCOUT_APP_NAME` là biến môi trường tùy chọn, mặc định `TrendScout API` và dùng làm tiêu đề FastAPI. Ví dụ: `export TREND_SCOUT_APP_NAME="TrendScout Local"` trước khi chạy server. Dùng `--host` và `--port` của Uvicorn để cấu hình địa chỉ và cổng. Không cần credential ở bước này. Tích hợp sau này phải nhận credential qua môi trường hoặc cơ chế bí mật phù hợp, không commit key.

## Cấu trúc

- `app/api/`: HTTP routes.
- `app/adapters/`: interface nguồn; adapter Wikimedia chưa được triển khai.
- `app/models/`: cấu trúc dữ liệu dự kiến, phân biệt giá trị thiếu với 0.
- `app/processing/`: vị trí dành cho validation, normalize, cleaning và dedup.
- `app/analysis/`: vị trí dành cho baseline và scoring.
- `app/storage/`: interface lưu snapshot; chưa có Firestore.
- `app/orchestration/`: vị trí dành cho điều phối run.

Các interface và model là nền tảng tạm, có thể điều chỉnh khi triển khai các bước kế tiếp; chưa xác nhận hợp đồng lưu trữ hoặc pipeline hoạt động. Không có dữ liệu giả, chưa deploy.
