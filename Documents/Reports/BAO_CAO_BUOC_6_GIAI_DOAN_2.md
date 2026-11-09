# Giai đoạn 2 — Bước 6: Taxonomy và rule-based classification

**Ngày:** 09/11/2026. **Baseline:** `0415268`. Phạm vi offline, không gọi Wikimedia/Firestore/AI, đổi schema, scoring, cleaning hoặc deploy.

## 1. Kiến trúc và taxonomy

`app/taxonomy/registry.py` định nghĩa taxonomy `topics-v1.0`: `Category` immutable gồm ID, display name, parent ID và status; mapping immutable, tra cứu ID/alias và con, từ chối ID không hợp lệ. ID là định danh, không dùng display name. `home_goods` là alias **đọc/mapping tường minh** sang `home_living`, không viết lại category trong run cũ.

| Category ID | Subcategory ID (suffix sau dấu `.`) |
|---|---|
| `entertainment` | `film`, `music`, `television`, `anime` |
| `celebrity_public_figures` | `performers`, `musicians`, `creators`, `public_figures` |
| `fashion` | `clothing`, `footwear`, `accessories`, `jewelry` |
| `home_living` | `appliances`, `kitchen`, `furniture`, `decor` |
| `technology` | `ai`, `software`, `gadgets`, `electronics` |
| `gaming` | `video_games`, `hardware`, `esports` |
| `beauty_personal_care` | `skincare`, `cosmetics`, `haircare` |
| `sports` | `athletes`, `teams`, `competitions`, `equipment` |
| `food_beverage` | `food`, `drinks`, `cooking` |
| `health_fitness` | `fitness`, `wellness`, `health` |
| `travel_lifestyle` | `destinations`, `experiences`, `lifestyle` |
| `business_finance` | `companies`, `markets`, `finance` |
| `news_politics` | `elections`, `government`, `policy`, `current_events` |
| `other`, `unclassified` | Không có subcategory bắt buộc. |

Chọn tên suffix `performers`, `markets`, `policy` theo hướng dẫn Giai đoạn 2; tên hiển thị phản ánh yêu cầu. Tất cả ID hiện `active`; cấu trúc có trường status cho thay đổi sau này. Chưa có bằng chứng nghiệp vụ cho phép tự thay label theo display name.

## 2. Classification và compatibility

`app/taxonomy/legacy.py` chứa nguyên quy tắc `title-rules-v0.1`, được `analysis/keywords.py` gọi và validate nhãn qua registry. Các categories/evidence và serialization cũ vẫn là `fashion`, `home_goods`, `other`, `unclassified`; `other` chỉ khi rule v0.1 có bằng chứng, không khớp vẫn unclassified. API/dashboard và Firestore không nhận field mới, run cũ không bị migration. `map_legacy_categories` chỉ đổi nhãn khi được gọi tường minh, từ chối ID ngoài bộ legacy.

`app/taxonomy/classifier.py` cung cấp classifier **opt-in**, `context-title-rules-v0.2`; chưa nối vào pipeline, API, dashboard hay storage. Chỉ áp dụng rule tiếng Anh khi source là Wikimedia Pageviews và project `en.wikipedia`. Mỗi rule có ID/version, category, subcategory tùy chọn, pattern với ranh giới, priority, giải thích; evidence ghi matched text, source/project, context `article_title` và priority. Title chuẩn hóa Unicode NFC. Khi title có media qualifier, rule fashion/appliance/election bị chặn; `(film)` là evidence cho entertainment/film. Nếu không đủ context hoặc evidence, trả unclassified. Priority xác định thứ tự evidence; media suppression là conflict policy. Không tạo rule yếu để làm đầy các nhóm.

Contract `ClassificationResult`: keyword ID, `entity_type`, topic categories đa nhãn, subcategories, taxonomy/classification version, evidence, classification status, review status, commercial relevance. Entity type hiện mặc định `unknown`; relevance `not_assessed`, không suy từ chủ đề hay trend score. Cấu trúc hỗ trợ các entity type được yêu cầu nhưng chưa triển khai nhận dạng thực thể; không đánh giá nhu cầu mua.

Ví dụ **synthetic**:

- `Café fashion week and washing machine` → `fashion`, `home_living`, subcategory `home_living.appliances`; evidence lần lượt `fashion_week`, `washing_machine`.
- `The Washing Machine (film)` → `entertainment.film`, evidence `film_qualifier`; không gán đồ gia dụng chỉ từ tên tác phẩm.
- `2024 United States presidential election` → `news_politics.elections`, evidence `election`.
- `Stephen Miller`, `Apple`, `Fashion` → `unclassified`: title thiếu context; với project hoặc source ngoài phạm vi rule cũng không tự gán.

## 3. File và test

- Mới: `Backend/app/taxonomy/__init__.py`, `registry.py`, `legacy.py`, `classifier.py`, `Backend/tests/test_taxonomy.py`, báo cáo này.
- Sửa: `Backend/app/analysis/keywords.py` chuyển rule v0.1 sang compatibility module; không đổi `KeywordResult`, API, Firestore hay golden expected.
- **85 pass, 0 fail, 0 skip** (76 test cũ + 9 test taxonomy). Kiểm tra hierarchy, ID/alias/unknown, multi-label, evidence/conflict, Unicode, source/project, unclassified, independent entity/relevance, filter legacy và serialization cũ; test dùng synthetic.
- Golden 7 tests pass với `PYTHONHASHSEED=1` và `983`; serialized output bằng nhau và SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, giữ baseline. `pip check`, shell/Node syntax pass. Các kiểm tra cloud chỉ là fake/mock của suite, không chứng minh WIF hay Firestore live.

## 4. Known issues và Bước 7

Rules v0.2 chỉ hỗ trợ vài tín hiệu cụ thể, không phủ toàn bộ taxonomy và chưa đánh giá trên tập gán nhãn; không coi absence của evidence là absence của trend hoặc cơ hội. Tên trang là ngữ cảnh hạn chế, nguy cơ false positive còn tồn tại; review status luôn unreviewed. Tiếng ngoài `en.wikipedia` giữ unclassified ở v0.2. Title v0.1 có thể có những nhãn khác với v0.2, do đó không tự thay version trên dữ liệu đã lưu.

Trước Bước 7, xác nhận policies cleaning/nguồn, phạm vi ngôn ngữ, dữ liệu test/giấy phép và ngưỡng chất lượng; giữ v0.1 golden, identity, missing/censored và API cũ. Các trang kỹ thuật còn có thể lọt cleaning vẫn là known issue từ demo. Không có AI, taxonomy endpoint, migration hay thay đổi sản phẩm online trong bước này.

**Đánh giá:** PASS cho taxonomy và rule-based classification trong phạm vi offline; classifier v0.2 là contract opt-in chưa được bật cho run hiện tại, không tuyên bố chất lượng phân loại hay cloud live.
