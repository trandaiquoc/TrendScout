# TrendScout — Báo cáo D4: SERP ngoại tuyến và US discovery

**Ngày:** 11/11/2026. **Phạm vi nghiệm thu:** thiết kế nghiệp vụ và adapter synthetic/offline; không phải nghiệm thu tích hợp live.

## Kết quả

- `D4_SERP_ADAPTERS_AND_US_PLATFORM_INVENTORY_OFFLINE_VERIFIED` — kiểm chứng ngoại tuyến và tài liệu D4 hoàn tất trong phạm vi danh mục đã khảo sát; các quyền và dữ liệu chưa xác minh được đánh dấu trong inventory.
- `FOUR_SOURCE_LIVE_SCOPE_DEFINED` — Wikimedia Pageviews, Google SERP qua SerpApi, Bing SERP qua SerpApi, YouTube Data API v3.
- `DISCOVERY_FIRST_BUSINESS_DESIGN_COMPLETED` — keyword là sản phẩm chính, Fashion/Home ưu tiên nghiên cứu, gợi ý sản phẩm là đầu ra phụ.
- `LIVE_FETCH_DISABLED_PENDING_D5_APPROVAL` — không phê duyệt hay xác minh live trong D4.
- `MULTI_SOURCE_LIVE_DEMO_NOT_YET_ACCEPTED` — chưa có bằng chứng thu thập live bốn nguồn, phối hợp nguồn hay UAT live.

**D4 Business Design: PASS** cho tài liệu và phạm vi được ghi nhận, không khẳng định danh sách có thể bao phủ vĩnh viễn mọi platform Mỹ. **D4 Engineering: PASS ngoại tuyến** cho code và regression dưới đây. Các status này không thay thế review policy, anti-abuse, quota hoặc chấp thuận live.

## 1. Baseline và bảo toàn Git

Trước sửa, local `main` và `git ls-remote origin refs/heads/main` đều là `00c156ec8dea6b839fd1eb46b07c69c1e509bd01`; worktree chính sạch, stash list trống. Kiểm tra bốn worktree: `TrendScout-stash-audit` có các thay đổi D3B staged/unstaged từ trước; hai worktree Vercel còn lại sạch. Các worktree này không bị sửa hay xóa. Không reset, rebase, rewrite history, push hoặc deploy.

## 2. Quyết định nghiệp vụ và danh mục

[Phân tích BA](../Design/US_TREND_KEYWORD_DISCOVERY_BUSINESS_ANALYSIS.md) mô tả `DISCOVER → EXTRACT → DETECT → VERIFY → EXPLAIN`, taxonomy đa nhãn, baseline, US evidence, source coverage, confidence, quota, cache, queue, data contract, journey, KPI đề xuất, rủi ro và decision register. 14 demo seeds là curated queries, không chứng minh coverage mọi topic tại Mỹ. SERP rank, YouTube result position và Wikimedia pageviews là các metric khác nhau; không cộng trực tiếp hay suy ra sales.

[Inventory](../Design/US_PLATFORM_SOURCE_INVENTORY.md) có **81 ID duy nhất**, gồm platform, surface và dataset/feed; số này không phải 81 provider hay 81 nguồn đang chạy. Bao phủ social, forums/messaging, video, Fashion/Home commerce, marketplace/retail, news/open-data và interest communities. Parent/surface, relevance, discovery/verification, priority, API feasibility, quyền, quota/cost và unknowns được ghi tại dòng hoặc metadata profile áp dụng chung. Nhiều mục chưa xác minh quyền, quota hoặc mức phổ biến US được giữ với `U`/pending thay vì tuyên bố khả dụng.

Pew 2026 là bằng chứng về người trưởng thành Mỹ tự báo cáo đã từng dùng một số nền tảng; nghiên cứu community Pew và US Census bổ sung bối cảnh cộng đồng và bán lẻ. Các tỷ lệ nhóm không được gán cho từng niche platform. Inventory liên kết tài liệu provider, ngày rà soát, điều kiện và quy trình cập nhật ít nhất hàng quý. [TikTok Research Tools](https://developers.tiktok.com/products/research-api/) có điều kiện nghiên cứu phi thương mại; [Reddit](https://redditinc.com/policies/data-api-terms) yêu cầu thỏa thuận cho commercial use; [Discord](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy) và [Pinterest](https://developers.pinterest.com/terms/) có hạn chế đáng kể. Các nhận định này không cấp quyền cho TrendScout.

## 3. Google/Bing và shared SerpApi

- Hai adapter chỉ nhận `httpx.MockTransport`, resolver được inject và synthetic responses. Không có live transport factory, environment resolver, API account hay credential thật; chúng không được đăng ký cho Public Fetch hoặc live registry.
- Fixed plan `demo-us-en` v1 vẫn 14 seeds; fingerprint `57f80bbd40ba13bed596f8f7e5fb30787f9cf5ea854cef32d9a4d5cf5713f5ac`. Request builder kiểm tra query context đã đăng ký, endpoint và parameter allowlist. Google dùng `start=0,10,…`, `gl=us`, `hl=en`, `google_domain=google.com`; Bing dùng `first=1` ban đầu, `mkt=en-US` và offset tiếp theo từ metadata đã kiểm tra. Không follow URL provider, không truyền các parameter khác trong URL đó.
- [SerpApi Google](https://serpapi.com/search-api) mô tả `tbs` tổng quát; `qdr:w` không được gửi khi ngữ nghĩa cho use case chưa được xác minh. [SerpApi Bing](https://serpapi.com/bing-search-api) mô tả `first` và ví dụ offset biến thiên; không giả định bước cố định hay dùng `start` cho Bing. Hai nguồn là sample theo query, không phải search volume, Google Trends, US audience hay doanh số.
- Shared pool reserve một request và một cost unit bảo thủ trước mỗi attempt, kể cả retry, cho cả hai engine; retry, `Retry-After`, body, page, result và deadline được giới hạn. Pool chỉ process-local, không chịu restart hoặc nhiều host và không phải bảo vệ public production. Số unit không phải hóa đơn provider.
- Canonical URL được kiểm tra và bỏ fragment/tracking thông dụng; dedup giữ evidence của từng query/page. Evidence gồm plan fingerprint, scope, offset/position, collection time, HTTP status, cache state và provider creation time nếu hợp lệ. Timestamp tương lai bị bỏ và gắn quality flag. Missing, malformed, empty, rejected, partial, repeated page, invalid next offset, rate/quota, timeout và cancellation được phân biệt; trang hợp lệ được giữ nếu next offset sai.
- SerpApi tài liệu hóa credential là query parameter; D4 chỉ dùng fake key trong MockTransport và không trả raw URL/body/key trong kết quả hoặc lỗi. D5 phải thiết kế redaction cho HTTP logs, proxy và tracing trước live. Pricing, account quota, retention và quyền đối với nội dung kết quả chưa được phê duyệt.

## 4. Tài liệu và D5 readiness

- [Capability matrix](../Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md) phân biệt offline adapter, approval, configuration, live verification và runtime catalog với strategic inventory.
- [D5 blueprint](../Design/D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md) giới hạn bốn nguồn, có checklist access, credential, endpoint, quota/cost, use/retention/attribution, live test, pagination, rate, US scope, freshness, partial failure, provenance và monitoring cho từng nguồn.
- D5 cần một môi trường execution phù hợp, durable queue/job state, distributed admission/idempotency, shared quota và cost reservation, bounded concurrency, cache theo quyền, abuse controls và monitoring. Vercel hiện read-only; bật POST đơn thuần không đủ bảo vệ public Fetch. Không có D3C và không bắt đầu D5 ở đây.

## 5. Kiểm thử và review

`bash Backend/scripts/verify_offline_readiness.sh`: **PASS**, sau sửa timestamp cuối.

| Kiểm tra | Kết quả |
|---|---|
| Backend unittest | 467 PASS, gồm 23 kiểm thử SerpApi offline riêng; D3A, D3B, legacy Bearer và Vercel read-only nằm trong suite |
| Golden | 7/7 với `PYTHONHASHSEED=1` và 7/7 với `983`; expected SHA-256 `712ba7dd31c93739064d155ef2aaa00ab5ed06a7bff0e87d7f8aa370ac86ffed` không đổi |
| Browser offline | Chrome 768/1024/1280/1440, keyboard và mobile 320/375; pipeline synthetic → API → dashboard; public one-click Fetch/polling/duplicate guard PASS |
| Packaging/dependencies | Vercel parity 11 assets; pip check, shell và JS syntax PASS; không thêm dependency |
| Isolation | Strict seccomp chặn AF_INET/AF_INET6/connect; browser chỉ cho AF_UNIX IPC. Không gọi provider API live, Firestore hoặc Firestore Emulator |
| Review bổ sung | Ruff trên file Python thay đổi PASS; `git diff --check` PASS; 81 ID duy nhất có category, 40 phần BA và tên platform yêu cầu được kiểm tra; liên kết Markdown nội bộ tồn tại |

Kiểm thử riêng bao gồm request allowlist, fake credentials và redaction, hai engine/nhiều query, dedup/evidence, offset biến thiên và link không tin cậy, repeated page, page cap, quota pool atomic theo thread, retries/Retry-After, deadline/cancellation, HTTP/provider errors, empty/malformed/oversized/result cap, cache provenance và timestamp tương lai. Browser E2E dùng Public Fetch Wikimedia synthetic hiện hữu; nó không chứng minh SERP live.

## 6. Rủi ro và blocker trước live

Provider approval, rights đối với kết quả, dữ liệu cá nhân, retention/deletion/attribution, YouTube derived metrics, quota thực, mức giá và khả năng truy cập từng nguồn chưa được xác minh cho TrendScout. SerpApi và search engines có thể thay đổi kết quả, pagination và pricing. Local budget không chống direct clients, bot phân tán, nhiều IP/process hoặc restart. Bốn nguồn không đại diện toàn bộ US; thiếu baseline và US evidence phải hiển thị rõ. Các mục `U` trong inventory cần nghiên cứu riêng trước mọi proposal tích hợp.

## 7. Local commits

- `21175ab6e608fd0a5cae4dafa5da49502c62bb6f` — business analysis, inventory và blueprint.
- `0da844e7f095dd3975f451f129420514ac40dede` — shared SerpApi contracts.
- `3a541220bcbdb864f9d5099b83b93ceec45a8cdd` — Google/Bing offline adapters và capability documentation.
- `cdbabb148406e656811839cc40ecab4fd91724d5` — offline tests và compatibility fixtures.
- `8178e68cecfd4934a0927700d7fcb3010e454b56` — review fix cho timestamp tương lai.

Commit chứa báo cáo này được cung cấp trong handoff cuối vì một commit không thể ghi SHA của chính nó vào nội dung. Không push, không deploy, không cấu hình Vercel production, không cấp Firestore permissions, không mua plan, không dùng credential thật và không chuyển sang D5.
