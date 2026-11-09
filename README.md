# TrendScout

Demo khám phá tín hiệu quan tâm qua Wikimedia Pageviews US/all-access. Backend Python 3.12 + FastAPI làm sạch, phân loại và tính điểm heuristic; dashboard hiển thị bằng chứng. Đây không phải xác suất, xu hướng mua sắm hoặc đo lường toàn bộ mạng xã hội.

## Chạy trên Ubuntu

Cần Python 3.12, `python3.12-venv`, ADC và một Firestore database đã được cấu hình. Từ repository:

```bash
cd Backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="trendscout-192e4"
# Tự đặt token tối thiểu 24 ký tự, nhập kín trong terminal; không gửi token vào chat.
read -rs -p "Operator token: " TREND_SCOUT_OPERATOR_TOKEN; printf '\n'
export TREND_SCOUT_OPERATOR_TOKEN
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở `http://127.0.0.1:8000/dashboard/`. Chọn **Fetch & Analyze** và nhập lại token để tạo run; không có fetch tự động khi mở trang. Token chỉ giữ tạm trong bộ nhớ trình duyệt. Giữ lại run ID để nhập vào **Mở run** sau khi restart. Xem [Backend README](Backend/README.md) để thử API bằng curl và biết cấu trúc Firestore; [Frontend README](Frontend/README.md) giải thích giao diện.

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

Chạy demo trên một host; khóa hiện tại không đồng bộ nhiều host. Kết quả live E2E, persistence sau restart và các giới hạn được ghi trong [báo cáo nghiệm thu demo](Documents/BAO_CAO_NGHIEM_THU_DEMO.md). Khi cần replay, có thể cân nhắc lưu snapshot gốc có provenance, ngày, license và nhãn `REPLAY` trong storage với giới hạn retention; chưa triển khai cơ chế replay ở đây.

## Tài liệu

- [Phân tích nghiệp vụ và thiết kế hệ thống tổng thể](Documents/TrendScout_Phan_Tich_Nghiep_Vu_Thiet_Ke_Tong_The.docx)
- [Thiết kế demo phân tích keyword](Documents/TREND_KEYWORD_DEMO_BUSINESS_SYSTEM_DESIGN.md)
- [Các bước hoàn thành demo](Documents/HUONG_DAN_CAC_BUOC_HOAN_THANH_DEMO.md)
- [Khảo sát dự án tham khảo](Documents/REQUIREMENTS_TECH_BA_DEV.md)
