# Giai đoạn 2 — Bước 4: Source Adapter Framework và Wikimedia Migration

**Thời điểm:** 09/11/2026. **Baseline:** `c9cccac` trên local và `origin/main`, đã xác nhận bằng `git fetch origin main`. Chỉ thay kiến trúc source, giữ hành vi demo; không gọi Wikimedia live, Firestore thật, AI hoặc deploy.

## 1. Kiến trúc trước và sau

Trước: `composition → app.adapters.wikimedia.WikimediaAdapter`; `RunService` nhận source factory qua DI, `CollectionSource.collect` là protocol. Metadata và implementation gắn với adapter; chỉ có một source cứng.

Sau:

```text
app/composition.py → source_registry() → SourceRegistry.factory(source_id)
                                      → WikimediaAdapter (tạo khi run fetch)
app/sources/
  base.py                  SourceMetadata + re-export contract hiện có
  registry.py              đăng ký, kiểm tra, lấy metadata/factory, create
  wikimedia/adapter.py     implementation Wikimedia duy nhất + metadata
app/adapters/wikimedia.py  alias tương thích đường dẫn cũ
app/models/observations.py CollectionResult / snapshot / observation (giữ nguyên)
```

Registry đăng ký rõ `wikimedia_pageviews_top_per_country` với factory lazy. ID rỗng, duplicate và source chưa đăng ký bị từ chối có lỗi; lookup/import không tự tạo adapter, SDK hay request. `create(id)` chỉ gọi factory đã đăng ký. Composition mặc định lấy factory Wikimedia từ registry; source fake vẫn có thể được inject. Không expose API quản lý source và không tạo adapter/folder giả cho Reddit, YouTube, Bluesky hoặc News.

`SourceMetadata` là dataclass immutable mô tả ID, tên, country, access, metric, diễn giải, capabilities và tên cấu hình. Wikimedia metadata công khai `US`, `all-access`, `views_ceil` xấp xỉ, daily snapshot, latest, seven-day history, top-list censored và tùy chọn User-Agent/retry/timeout. Đây là metadata của source hiện tại, không chuẩn hóa các metric khác đơn vị hoặc tạo score thương mại. `SourceAdapter`, `CollectionSource`, `CollectionResult` được tái sử dụng; snapshot/result vẫn mang ngày, request count, status/errors, project/article, raw/provenance. Window của Wikimedia giữ trong contract `collect` hiện có; framework chưa áp đặt window chung cho source khác.

## 2. Migration và tương thích

Implementation được chuyển nguyên từ `app/adapters/wikimedia.py` sang `app/sources/wikimedia/adapter.py`; chỉ bổ sung metadata. Các phương thức fetch, URL, User-Agent, US/all-access, xử lý 200/404/429/timeout/retry và bảy ngày lịch sử giữ nguyên. Không lọc category hoặc chỉnh cleaning, taxonomy, score, stable IDs, Firestore schema hay routes.

Đường cũ `from app.adapters.wikimedia import WikimediaAdapter` và các symbols `CollectionResult`, `SOURCE`, `BASE_URL`, `DEFAULT_USER_AGENT`, `observation_status`, `_parse` vẫn tồn tại. Compatibility module alias chia sẻ **cùng module object** với implementation mới, nên patch module globals trong tests/caller cũ vẫn có hiệu lực; không có hai bản implementation. Test kiểm tra identity cả module lẫn symbols. Dependency cũ `analysis/trend.py → app.adapters.wikimedia.SOURCE` vẫn hoạt động qua alias; việc tách metric/source-specific scorer thuộc bước sau. Alias dựa vào `sys.modules`, cần giữ test compatibility khi đổi lại cấu trúc import.

## 3. File thay đổi

- Mới: `Backend/app/sources/__init__.py`, `base.py`, `registry.py`, `wikimedia/__init__.py`, `wikimedia/adapter.py`; `Backend/tests/test_source_registry.py`; báo cáo này.
- Sửa: `Backend/app/adapters/wikimedia.py` thành compatibility alias; `Backend/app/composition.py` đăng ký và chọn Wikimedia factory. Source implementation cũ được chuyển, không sao chép song song.
- Không thay fixture `golden_v1/expected.json`, storage, API, dashboard, setup/start hoặc Vercel config.

## 4. Kiểm thử thực tế

- Toàn bộ backend: **67 pass, 0 fail, 0 skip**, gồm 62 test cũ + 5 test registry/compatibility mới. `pip check`, `bash -n setup.sh start.sh` và `node --check Frontend/public/app.js` pass.
- 7 golden tests pass riêng ở `PYTHONHASHSEED=1` và `983`; output đầy đủ ở hai process giống nhau, SHA-256 `63c534739c8c8980a97435312ab1b16d71b98665ff726e5dfb414a8e83395aac`, khớp Bước 2–3; không đổi expected.
- Test mới kiểm tra duplicate/unsupported, metadata, fake factory, lazy creation, alias import/symbol identity, import trong process mới với socket connect bị chặn. Test cũ tiếp tục kiểm tra source fake, scope/date window, 200/404/429/timeout và bounded retry, missing/censored, IDs, request metadata, provenance, API và schema fake.
- Local/test mode được kiểm tra bằng fake/ASGI; read-only tests xác nhận không tạo collector/lock, bảo vệ auth và POST. WIF/OIDC chỉ mock; không có kiểm tra Vercel build/deploy hay Firestore live. Không truy cập run live cũ. `./start.sh` chỉ được kiểm tra syntax trong bước này.

## 5. Giới hạn, rollback và Bước 5

- Registry hiện chỉ có Wikimedia, metadata/contract còn có các trường đặc thù nguồn; source mới phải có quyền API/thương mại, metric, dữ liệu và contract test riêng trước khi thêm. `collect` của source hiện tại có window defaults; chưa có framework chung xử lý policy/window khác nhau.
- `views_ceil` chỉ là cận trên xấp xỉ trong top; 404/không lọt top/missing không được biến thành 0. Một số technical pages có thể còn lọt cleaning theo báo cáo demo; chưa sửa ở bước này. Host lock chỉ một host. WIF cloud, IAM, provider và chi phí chưa được kiểm chứng ở bước này.
- Rollback bằng revert commit refactor và báo cáo, không rewrite history; code trước vẫn dùng import path cũ. Không cần migration Firestore vì schema giữ nguyên. Nếu push lên GitHub main, project Vercel có thể tự build nếu đã liên kết; không chủ động deploy trong phiên này.
- Có thể chuẩn bị Bước 5 sau khi xác nhận working tree/remote và duyệt phạm vi repository contract/migration; chạy lại golden và kiểm tra shards cũ với fake/emulator trước mọi thay đổi storage.

**Đánh giá:** PASS trong phạm vi offline; không đồng nghĩa đã nghiệm thu cloud hoặc commercial readiness.
