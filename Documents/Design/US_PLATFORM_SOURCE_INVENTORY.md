# Danh mục chiến lược nguồn khám phá keyword tại Mỹ

**Ngày tổng hợp:** 11/11/2026. Đây là kiểm kê ứng viên, không phải danh sách vĩnh viễn hoặc runtime catalog. Không có nguồn tương lai nào được bật live bằng tài liệu này. Không thể chứng minh đã bao phủ mọi cộng đồng Mỹ; các khoảng trống và nguồn chưa xác minh được giữ lại để nghiên cứu tiếp. `US` dưới đây là mức phù hợp nghiệp vụ định tính, không là ước tính thị phần hoặc bằng chứng người dùng/nội dung ở Mỹ.

## Phương pháp, bằng chứng và cách đọc

[Pew 2026](https://www.pewresearch.org/internet/fact-sheet/social-media/) khảo sát 5.511 người trưởng thành Mỹ, 30/1–18/6/2026; các tỷ lệ là tự báo cáo **đã từng sử dụng**, không phải hoạt động hằng ngày hay dữ liệu keyword. Các giá trị trong bảng: YouTube 84%, Facebook 70%, Instagram 51%, TikTok 37%, WhatsApp 33%, LinkedIn 31%, Snapchat 26%, Reddit 25%, X 18%, Nextdoor 18%, Truth Social 4%. Ô Pew không có năm 2026 không được điền bằng số cũ. Pew không đo mọi community; các mục còn lại được giữ theo mức phù hợp chủ đề/hoạt động công khai, chưa có ước lượng US độc lập trong lần này. Link sản phẩm chính thức giúp xác nhận loại dịch vụ, **không tự chứng minh quyền thu thập hoặc quy mô US**.

[Pew về cộng đồng trực tuyến](https://www.pewresearch.org/internet/2026/10/14/online-community-and-connection-in-america/) đo nhóm người Mỹ tham gia community, nêu Reddit/Nextdoor/Discord, YouTube/Twitch và các cộng đồng Goodreads, Strava, Ravelry, Letterboxd như ví dụ; số liệu theo nhóm không phải số người dùng từng platform. [US Census](https://www.census.gov/retail/eCommerce.html) ước tính thương mại điện tử chiếm 17,1% doanh số bán lẻ Mỹ điều chỉnh mùa vụ quý 2/2026; đây là bối cảnh cho nhóm retail, không chứng minh doanh số của bất kỳ listing hay platform nào.

Mỗi dòng là một platform/dataset hoặc surface có mã ổn định. Cột `F/H` là tiềm năng Fashion/Home (`C` cao, `V` vừa, `T` thấp; phán đoán nghiệp vụ), không phải API availability. `D` discovery, `V` verification, `H` hybrid; P1/P2/P3 là ưu tiên nghiên cứu, không phê duyệt live. Nội dung là loại có thể hữu ích nếu được phép. Các surface Groups, Communities, Reels và Spotlight nằm trong dòng parent và không tính thêm provider/quota độc lập; TikTok Shop và Facebook Marketplace được tách làm surface thương mại để đánh giá quyền nhưng không coi là API/quota độc lập đã xác minh.

### Metadata áp dụng cho từng dòng

| Mã feasibility | Availability, điều kiện và authentication | Commercial use, quota/cost, pagination | Geo, freshness, retention, attribution, privacy và adapter |
|---|---|---|---|
| `U` | Chưa xác minh API/feed/dataset phù hợp discovery; không mặc định scraping. Auth/access chưa xác minh. | Quyền thương mại, quota, giá và pagination chưa xác minh; cần provider xác nhận. | Geo/freshness chưa xác minh; không lưu/phát lại trước review retention và attribution. Nội dung có thể chứa dữ liệu cá nhân; adapter chỉ đề xuất sau policy review. |
| `G` | Có developer/API hoặc chương trình truy cập được dẫn; scope, auth, quyền và eligibility tùy endpoint/partner. | Dùng thương mại **chưa được TrendScout phê duyệt**; quota/giá/page limit theo tài khoản và endpoint, trừ ngoại lệ nêu dưới. | Location của nội dung/audience và freshness không mặc định là US; retention, attribution, deletion và privacy cần review cụ thể. Adapter riêng với credential server, rate/page caps và provenance sau approval. |
| `F` | Feed/dataset công khai hoặc được cấp phép theo publisher; xác thực và điều kiện thay đổi theo nguồn. | Quyền thương mại, rate, cost, pagination và redistribution cần xác minh theo feed/license, không mặc định miễn phí hoặc toàn diện. | Ghi publisher, scope, timestamp, license/TTL, attribution và deletion; tôn trọng quyền riêng tư; adapter feed/dataset riêng sau approval. |
| `R` | Runtime capability có contract trong repo, nhưng live chưa được phê duyệt; credential và scope theo bảng chi tiết. | D4 chỉ synthetic; quota/cost thực chưa xác minh trên tài khoản. | Ghi filter, collection time, cache provenance và sampling; retention/attribution chờ review. Adapter source-specific; không đăng ký vào Public Fetch live. |

Mặc định cho **mọi dòng**: `approval=PENDING_APPROVAL`, `live_verification=NOT_VERIFIED`, `enabled=DISABLED`; các dòng ngoài `R` có `implementation=NOT_IMPLEMENTED`, trạng thái nghiên cứu `DISCOVERED`/`RESEARCHED` theo link và kết quả đã xem, không mặc định có access. `R`: Wikimedia có adapter hiện hữu; YouTube/Google/Bing `IMPLEMENTED_OFFLINE_VERIFIED` theo regression D4 khi bộ test đạt. Tất cả được rà soát metadata ngày 11/11/2026; `U` nghĩa là review chưa đủ để xác nhận API, không có nghĩa API chắc chắn không tồn tại. Alias `—` nghĩa không xác minh alias khác; parent `?` nghĩa chưa xác minh. Các hạn mức, giá, permission, retention, attribution hoặc khả năng pagination không được nêu cụ thể vẫn là **chưa xác minh**, không được điền bằng giả định.

Phân loại chính của từng ID sau đây bổ sung cho phần nội dung từng dòng; một platform có thể có nhiều surface nhưng chỉ được tính một lần trong nhóm tương ứng:

| Platform category | Stable IDs |
|---|---|
| Social network / media | `facebook`, `instagram`, `x`, `threads`, `snapchat`, `pinterest`, `linkedin`, `nextdoor`, `bluesky`, `truth_social`, `bereal`, `mastodon`, `tumblr`, `lemon8` |
| Community / forum / messaging | `reddit`, `whatsapp`, `discord`, `telegram`, `quora`, `stack_exchange`, `slack`, `lemmy`, `discourse`, `fandom` |
| Video / streaming / creator | `youtube`, `tiktok`, `twitch`, `kick`, `rumble`, `vimeo`, `dailymotion`, `patreon` |
| Social commerce | `poshmark`, `depop`, `whatnot`, `tiktok_shop` |
| E-commerce marketplace / local marketplace | `etsy`, `mercari`, `offerup`, `facebook_marketplace`, `amazon`, `walmart`, `ebay`, `temu` |
| Product discovery / retail catalog | `shein`, `shop_app`, `wayfair`, `houzz`, `home_depot`, `lowes`, `target`, `nordstrom`, `google_shopping` |
| News / technology / open-data discovery | `hacker_news`, `product_hunt`, `substack`, `medium`, `google_news`, `google_trends`, `gdelt`, `public_rss`, `common_crawl`, `wikimedia_pageviews`, `google_serp`, `bing_serp`, `newsapi`, `github`, `openalex` |
| Entertainment / interest community | `roblox`, `steam_community`, `goodreads`, `letterboxd`, `soundcloud`, `spotify`, `apple_podcasts`, `podcast_index`, `yelp`, `tripadvisor`, `strava`, `ravelry`, `alltrails` |

## A. Mainstream social và community

| Stable ID | Official name / alias / parent | Nội dung, US evidence; F/H; vai trò, ưu tiên | Feasibility và tài liệu gốc |
|---|---|---|---|
| `facebook` | Facebook / FB, Groups, Reels, Marketplace / Meta | Post, group, video; Pew 70%; C/C; H P1 | G; [Meta developers](https://developers.facebook.com/). Group bị hạn chế không mặc định truy cập. |
| `instagram` | Instagram / IG, Reels / Meta | Ảnh, video, hashtag; Pew 51%; C/C; H P1 | G; [Instagram platform](https://developers.facebook.com/docs/instagram-platform/); scopes/use case cần duyệt. |
| `youtube` | YouTube / Shorts / Google | Video và search; Pew 84%; C/C; H P1 | R; [Data API](https://developers.google.com/youtube/v3/docs/search/list); D3B synthetic. |
| `tiktok` | TikTok / — / TikTok | Video, hashtag; Pew 37%; C/C; H P1 | G; [Research Tools](https://developers.tiktok.com/products/research-api/) yêu cầu điều kiện phi thương mại; [Commercial Content API](https://developers.tiktok.com/products/commercial-content-api) nêu phạm vi EU, chưa xác nhận phù hợp US. |
| `x` | X / Twitter / X Corp | Post, links; Pew 18%; V/V; H P1 | G; [X API](https://docs.x.com/); access và giá theo tier cần xác nhận. |
| `threads` | Threads / — / Meta | Bài viết; Pew có đo nhưng không có số 2026 trong bảng; V/V; D P2 | G; [Threads API](https://developers.facebook.com/docs/threads/); discovery scope cần xác minh. |
| `reddit` | Reddit / subreddits, communities / Reddit | Bài và bình luận; Pew 25%; C/C; H P1 | G; [Data API terms](https://redditinc.com/policies/data-api-terms): thương mại cần thỏa thuận riêng; nội dung/retention có hạn chế. |
| `snapchat` | Snapchat / Snap, Spotlight / Snap | Ảnh, video; Pew 26%; C/V; D P2 | G; [Snap developers](https://developers.snap.com/); Spotlight là surface, quyền discovery chưa xác minh. |
| `pinterest` | Pinterest / Pins / Pinterest | Ý tưởng hình ảnh; Pew có đo, thiếu số 2026; C/C; H P1 | G; [terms](https://developers.pinterest.com/terms/) hạn chế lưu và platform insights nếu thiếu quyền; [rate limits](https://developers.pinterest.com/docs/reference/rate-limits/) theo tier. |
| `linkedin` | LinkedIn / — / Microsoft | Bài nghề nghiệp; Pew 31%; V/V; H P2 | G; [access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access), OAuth và nhiều quyền cần approval; không giả định public search rộng. |
| `whatsapp` | WhatsApp / Communities / Meta | Nhắn tin, communities; Pew 33%; V/V; D P3 | G; [WhatsApp platform](https://developers.facebook.com/docs/whatsapp/); business API không cấp quyền đọc mọi private conversation/community. |
| `nextdoor` | Nextdoor / — / Nextdoor | Cộng đồng địa phương; Pew 18%; V/C; D P2 | U; [Nextdoor](https://about.nextdoor.com/); quyền và geography theo community chưa xác minh. |
| `bluesky` | Bluesky / AT Protocol / Bluesky | Post/feed; Pew có đo, không có số 2026; V/V; H P2 | G; [API](https://docs.bsky.app/docs/api/app-bsky-feed-get-feed) mô tả public reads; commercial, quota, retention và US scope cần review. |
| `truth_social` | Truth Social / — / Trump Media | Post; Pew 4%; T/T; D P3 | U; [Truth Social](https://truthsocial.com/); access commercial chưa xác minh. |
| `bereal` | BeReal / — / ? | Ảnh; Pew có đo, không có số 2026; V/V; D P3 | U; [BeReal](https://bereal.com/); discovery access chưa xác minh. |
| `discord` | Discord / servers / Discord | Chat/community; niche gaming và sở thích; V/V; D P2 | G; [privileged intents](https://support-dev.discord.com/hc/en-us/articles/6207308062871-What-are-Privileged-Intents), [policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy). Không phải kho tin nhắn công khai tải toàn bộ; policy cấm scraping và thương mại hóa API data. |
| `telegram` | Telegram / groups, channels / Telegram | Kênh và chat; niche community; V/V; D P3 | G; [API](https://core.telegram.org/api); private groups không mặc định có quyền thương mại. |
| `mastodon` | Mastodon / federated instances / phi tập trung | Post liên bang; niche; V/V; D P2 | G; [API](https://docs.joinmastodon.org/api/); quyền, scope và coverage theo instance. |
| `tumblr` | Tumblr / — / Automattic | Blog và media; niche creator; V/V; D P2 | G; [API](https://www.tumblr.com/docs/en/api/v2); quyền, quota và commercial review còn mở. |
| `quora` | Quora / — / Quora | Hỏi đáp; niche research; V/V; V P3 | U; [Quora](https://www.quora.com/); không giả định API discovery phù hợp. |
| `stack_exchange` | Stack Exchange / Stack Overflow / Stack Exchange | Hỏi đáp kỹ thuật; niche; T/T; H P2 | G; [API](https://api.stackexchange.com/); review license và attribution từng nội dung. |
| `slack` | Slack / public professional communities / Salesforce | Kênh nghề nghiệp; niche; V/V; D P3 | G; [Slack API](https://api.slack.com/); workspace consent không tạo quyền quét toàn nền tảng. |
| `lemmy` | Lemmy / federated communities / phi tập trung | Forum; niche; V/V; D P3 | U; [Lemmy](https://join-lemmy.org/); access và policy theo instance. |
| `discourse` | Discourse / forum instances / Civilized Discourse Construction Kit | Forum; niche; V/V; D P3 | G; [API](https://docs.discourse.org/); consent và terms theo instance. |
| `fandom` | Fandom / wikis / Fandom | Wiki cộng đồng; niche giải trí; T/T; D P3 | U; [Fandom](https://www.fandom.com/); license/coverage cần xác minh. |

## B. Video, streaming và creator

| Stable ID | Official name / alias / parent | Nội dung, relevance; F/H; vai trò, ưu tiên | Feasibility và tài liệu gốc |
|---|---|---|---|
| `twitch` | Twitch / — / Amazon | Stream/game/creator; niche US; V/V; D P2 | G; [API](https://dev.twitch.tv/docs/api/); auth và terms theo endpoint. |
| `kick` | Kick / — / ? | Livestream; niche; T/T; D P3 | U; [Kick](https://kick.com/); API commercial discovery chưa xác minh. |
| `rumble` | Rumble / — / Rumble | Video; Pew đo news use, không suy ra dân số toàn bộ; T/T; D P3 | U; [Rumble](https://rumble.com/); access chưa xác minh. |
| `vimeo` | Vimeo / — / Vimeo | Video creator; niche; V/V; D P3 | G; [API](https://developer.vimeo.com/api/guides/start); quyền từng endpoint cần duyệt. |
| `dailymotion` | Dailymotion / — / ? | Video; US relevance chưa định lượng; T/T; D P3 | G; [API](https://developers.dailymotion.com/); quyền và quota chưa xác minh. |
| `lemon8` | Lemon8 / — / ? | Lifestyle, ảnh/video; niche Fashion/Home; C/C; D P2 | U; [Lemon8](https://www.lemon8-app.com/); ownership, US access và API cần xác minh. |
| `patreon` | Patreon / — / Patreon | Creator và membership; niche; V/V; D P3 | G; [API](https://docs.patreon.com/); nội dung thành viên/consent không mặc định công khai. |

## C. Fashion, Home và shopping

Listing và search rank là evidence về nội dung được hiển thị, không là volume bán hàng. Không tự scrape giá, review hay listing. `C/C` gồm clothing, footwear, accessories, bags, home organization, kitchen, storage, decor và household products tùy catalog.

| Stable ID | Official name / alias / parent | Loại, US relevance; F/H; vai trò, ưu tiên | Feasibility và tài liệu gốc |
|---|---|---|---|
| `etsy` | Etsy / — / Etsy | Marketplace handmade; niche Fashion/Home; C/C; H P1 | G; [Open API](https://developers.etsy.com/) app/key, commercial review; screen-scraping bị cấm; [limits](https://developers.etsy.com/documentation/essentials/rate-limits/). |
| `poshmark` | Poshmark / — / ? | Social commerce resale; niche Fashion; C/T; D P1 | U; [Poshmark](https://poshmark.com/); API và parent cần xác minh. |
| `depop` | Depop / — / Etsy | Social commerce resale; niche Fashion; C/T; D P1 | U; [Depop](https://www.depop.com/); quyền discovery chưa xác minh. |
| `whatnot` | Whatnot / — / Whatnot | Social commerce livestream; niche; C/V; D P2 | U; [Whatnot](https://www.whatnot.com/). |
| `mercari` | Mercari / — / Mercari | Marketplace resale; niche US; C/C; D P2 | U; [Mercari US](https://www.mercari.com/). |
| `offerup` | OfferUp / — / OfferUp | Local marketplace; niche US; V/C; D P2 | U; [OfferUp](https://offerup.com/). |
| `tiktok_shop` | TikTok Shop / TikTok commerce surface / TikTok | Social commerce; Fashion/Home; C/C; D P1 | G; [Shop developers](https://partner.tiktokshop.com/); quyền seller/partner không mặc định là market-wide access. |
| `facebook_marketplace` | Facebook Marketplace / Meta surface / Meta | Local marketplace; Fashion/Home; C/C; D P1 | U; [Marketplace](https://www.facebook.com/marketplace/); không coi là nguồn API độc lập được duyệt. |
| `amazon` | Amazon / Amazon.com / Amazon | Marketplace/retail catalog; US relevance cao định tính; C/C; V P1 | G; [Amazon developers](https://developer.amazon.com/); chương trình, quyền catalog và use case cần xác minh. |
| `walmart` | Walmart / Walmart Marketplace / Walmart | Marketplace/retail; US relevance cao định tính; C/C; V P1 | G; [Marketplace API](https://developer.walmart.com/global-marketplace/page/getting-started) dành seller/delegated solution provider; không mặc định catalog toàn sàn. |
| `ebay` | eBay / — / eBay | Marketplace; US relevance định tính; C/C; H P1 | G; [Browse API](https://developer.ebay.com/develop/api/buy/browse_api) search listings, application token; quyền và quota production cần review. |
| `shein` | SHEIN / — / ? | Retail/marketplace fashion; US relevance chưa định lượng; C/V; V P2 | U; [SHEIN US](https://us.shein.com/); quyền API/scrape chưa xác minh. |
| `temu` | Temu / — / PDD Holdings | Marketplace; US relevance chưa định lượng; C/C; V P2 | U; [Temu](https://www.temu.com/); quyền API/scrape chưa xác minh. |
| `shop_app` | Shop / Shopify Shop / Shopify | Product discovery và merchant commerce; C/C; H P2 | G; [Shopify API](https://shopify.dev/docs/api); merchant scope không mặc định toàn bộ Shop. |
| `wayfair` | Wayfair / — / Wayfair | Retail catalog Home; C/C; V P1 | U; [Wayfair](https://www.wayfair.com/); partner quyền discovery chưa xác minh. |
| `houzz` | Houzz / — / Houzz | Home community và product discovery; V/C; H P1 | U; [Houzz](https://www.houzz.com/); access thương mại chưa xác minh. |
| `home_depot` | The Home Depot / Home Depot / Home Depot | Retail catalog Home; T/C; V P1 | U; [Home Depot](https://www.homedepot.com/); permission catalog chưa xác minh. |
| `lowes` | Lowe's / Lowes / Lowe's | Retail catalog Home; T/C; V P1 | U; [Lowe's](https://www.lowes.com/); permission catalog chưa xác minh. |
| `target` | Target / — / Target | US retail catalog; C/C; V P2 | U; [Target](https://www.target.com/); permission chưa xác minh. |
| `nordstrom` | Nordstrom / — / Nordstrom | US fashion retail; C/V; V P2 | U; [Nordstrom](https://www.nordstrom.com/); permission chưa xác minh. |
| `google_shopping` | Google Shopping / — / Google | Product discovery; C/C; V P2 | G; [Merchant API](https://developers.google.com/merchant/api); merchant-owned data không mặc định là toàn bộ Shopping. |

## D. News, technology, open data và search

| Stable ID | Official name / alias / parent | Nội dung, relevance; F/H; vai trò, ưu tiên | Feasibility và tài liệu gốc |
|---|---|---|---|
| `hacker_news` | Hacker News / HN / Y Combinator | Tin, thảo luận kỹ thuật; niche; T/T; D P2 | G; [official API](https://github.com/HackerNews/API); content license và US scope cần review. |
| `product_hunt` | Product Hunt / PH / ? | Sản phẩm công nghệ; niche; V/V; D P2 | G; [API](https://api.producthunt.com/v2/docs); permission và commercial use cần review. |
| `substack` | Substack / — / Substack | Newsletter/creator; niche; V/V; D P2 | F; [Substack](https://substack.com/); từng feed, bản quyền và paywall cần kiểm tra. |
| `medium` | Medium / — / Medium | Bài viết; niche; V/V; D P3 | F; [Medium](https://medium.com/); feed/license theo tác giả. |
| `google_news` | Google News / — / Google | Tin được tổng hợp; US edition có bias; V/V; H P2 | U; [Google News](https://news.google.com/); access/retention và publisher rights cần xác minh. |
| `google_trends` | Google Trends / — / Google | Search interest; US scope có thể chọn; C/C; H P1 | G; [API alpha](https://developers.google.com/search/apis/trends) giới hạn tester, không mặc định được cấp. Interest không là sales. |
| `gdelt` | GDELT / — / GDELT Project | News metadata; toàn cầu, geo inference hạn chế; V/V; D P2 | F; [GDELT](https://www.gdeltproject.org/); dataset scope và rights theo nội dung. |
| `public_rss` | Public RSS / publisher feeds / nhiều publisher | Tin/bài theo publisher; US tùy nguồn; V/V; D P2 | F; [RSS specification](https://www.rssboard.org/rss-specification); feed công khai không tự cấp quyền commercial. |
| `common_crawl` | Common Crawl / — / Common Crawl Foundation | Web crawl dataset; rộng nhưng không đại diện US; V/V; D P3 | F; [data](https://commoncrawl.org/get-started); rights từng website và freshness cần review. |
| `wikimedia_pageviews` | Wikimedia Pageviews / Wikipedia top list / Wikimedia Foundation | US country top-list; broad, censored; V/V; D P1 | R; [AQS](https://wikitech.wikimedia.org/wiki/Analytics/AQS/Pageviews); pageviews không là unique audience hay intent. |
| `google_serp` | Google Search / Google web / Google qua SerpApi | Query SERP; US localization; C/C; V P1 | R; [SerpApi Google](https://serpapi.com/search-api), shared account với Bing; rank không là volume. |
| `bing_serp` | Bing Search / Bing web / Microsoft qua SerpApi | Query SERP; en-US market; C/C; V P1 | R; [SerpApi Bing](https://serpapi.com/bing-search-api); không dùng API Bing Search cũ của Microsoft. |
| `newsapi` | News API / NewsAPI.org / ? | Tin qua dịch vụ thương mại; scope theo hợp đồng; V/V; H P3 | G; [documentation](https://newsapi.org/docs); quyền publisher, giá và attribution cần xem hợp đồng. |
| `github` | GitHub / — / Microsoft | Repositories, issues; niche kỹ thuật; T/T; D P2 | G; [REST API](https://docs.github.com/en/rest); license từng repo, auth/quota và US scope cần review. |
| `openalex` | OpenAlex / — / OurResearch | Metadata nghiên cứu; toàn cầu; T/T; D P3 | F; [API](https://docs.openalex.org/); license, quota và US evidence cần kiểm tra. |

## E. Entertainment và sở thích

| Stable ID | Official name / alias / parent | Nội dung, relevance; F/H; vai trò, ưu tiên | Feasibility và tài liệu gốc |
|---|---|---|---|
| `roblox` | Roblox / — / Roblox | Games và creator; niche US; V/V; D P2 | G; [Open Cloud](https://create.roblox.com/docs/cloud); entitlement và privacy, nhất là trẻ em, cần review. |
| `steam_community` | Steam Community / Steam / Valve | Game/community; niche; T/T; D P2 | G; [Steam Web API](https://partner.steamgames.com/doc/webapi_overview); quyền và coverage cần xác minh. |
| `goodreads` | Goodreads / — / Amazon | Sách/review; niche; T/V; D P3 | U; [Goodreads](https://www.goodreads.com/); API hiện phù hợp chưa xác minh. |
| `letterboxd` | Letterboxd / — / ? | Phim/review; niche; T/T; D P3 | U; [Letterboxd](https://letterboxd.com/); quyền commercial chưa xác minh. |
| `soundcloud` | SoundCloud / — / SoundCloud | Âm nhạc/creator; niche; T/T; D P3 | G; [API](https://developers.soundcloud.com/docs/api/guide); access và rights cần review. |
| `spotify` | Spotify / charts / Spotify | Âm nhạc và podcast; niche US; T/T; H P2 | G; [Web API](https://developer.spotify.com/documentation/web-api); chart/data use và policy cần review riêng. |
| `apple_podcasts` | Apple Podcasts / — / Apple | Podcast charts/feed; niche US; V/V; D P3 | F; [Apple Podcasts](https://podcasters.apple.com/); quyền chart/feed theo nguồn cần kiểm tra. |
| `podcast_index` | Podcast Index / — / Podcast Index | Podcast directory/feed; niche; V/V; D P3 | G; [API](https://podcastindex-org.github.io/docs-api/); từng feed có quyền riêng. |
| `yelp` | Yelp / — / Yelp | Local business/reviews; niche US; T/V; H P3 | G; [Fusion API](https://docs.developer.yelp.com/); reviews và commercial terms cần review. |
| `tripadvisor` | Tripadvisor / — / Tripadvisor | Travel/review; US relevance theo location; T/V; H P3 | G; [Content API](https://tripadvisor-content-api.readme.io/); access, quota và rights cần review. |
| `strava` | Strava / — / Strava | Fitness/activity; niche; V/T; D P3 | G; [API](https://developers.strava.com/); người dùng consent, privacy và allowed use cần review. |
| `ravelry` | Ravelry / — / Ravelry | Craft/yarn community; niche Fashion/Home; C/C; D P2 | U; [Ravelry](https://www.ravelry.com/); API và commercial access cần xác minh. |
| `alltrails` | AllTrails / — / AllTrails | Outdoor/trails; niche; V/T; D P3 | U; [AllTrails](https://www.alltrails.com/); rights và API cần xác minh. |

## Ngoại lệ policy đã đối chiếu

- TikTok Research Tools dành ứng viên đủ điều kiện phi thương mại; [FAQ](https://developers.tiktok.com/docs/en/research-api-faq) nêu quota nghiên cứu 1.000 request/ngày và data delay có thể có, không phải quota TrendScout. Commercial Content API được mô tả có scope EU tại thời điểm rà soát, không là nguồn US Phase 1.
- [Reddit terms](https://redditinc.com/policies/data-api-terms) yêu cầu thỏa thuận cho commercial use và giới hạn mục đích, deletion và attribution. [Discord policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy) hạn chế commercializing API data, mining/scraping và AI training; bot trong một server không tạo quyền truy cập mọi server.
- [Pinterest terms](https://developers.pinterest.com/terms/) hạn chế lưu phần lớn API data và platform/competitor insights khi thiếu authorization; [rate limits](https://developers.pinterest.com/docs/reference/rate-limits/) công bố Trial 1.000 request/ngày toàn app, từng category có giới hạn riêng. Không suy ra quyền lưu trend snapshots.
- [Etsy](https://developers.etsy.com/) yêu cầu app, API key, OAuth cho private scope, review commercial access và tuân thủ caching/attribution; không screen-scrape. [eBay Browse](https://developer.ebay.com/develop/api/buy/browse_api) dùng application token. [Walmart Marketplace](https://developer.walmart.com/global-marketplace/page/getting-started) mô tả seller/delegated access; không suy ra quyền lấy toàn catalog.
- [SerpApi legal](https://serpapi.com/legal) nêu search data provider lưu 31 ngày theo policy và trách nhiệm sử dụng nội dung; điều này không tự cho phép TrendScout lưu/hiển thị nội dung bên thứ ba. [Pricing](https://serpapi.com/pricing) tính successful searches theo gói, cached/error có quy tắc riêng. Google/Bing cùng pool; quota thật chưa được xác nhận.

## Governance và các khoảng trống

Data steward rà soát tối thiểu hàng quý và khi provider đổi API, policy, giá hoặc market; ghi ngày, owner, evidence, thay đổi, access/rights, hiệu lực và phiên bản. Bổ sung platform từ khảo sát US mới, cộng đồng ngành, publisher và supplier; phân biệt quy mô lớn và niche, không loại chỉ vì thiếu public API. Mỗi candidate phải được thử nghiệm khả thi và rights theo use case trước khi chuyển `RESEARCHED → PENDING_APPROVAL → IMPLEMENTED_OFFLINE_VERIFIED → LIVE_CONFIGURED → LIVE_VERIFIED`; `ACCESS_RESTRICTED`/`DISABLED` có thể áp dụng ở bất kỳ bước. Review phải bao gồm terms, authentication, commercial permission, quota/giá, pagination, geography, freshness, retention/deletion, attribution, privacy, architecture và provenance. Kiểm tra nội dung do người dùng tạo và trẻ vị thành niên, tuyệt đối không suy ra quyền truy cập private conversations/groups. Link nền tảng và mức relevance định tính ở các dòng `U` là đầu vào điều tra, không thay cho xác minh API hay số liệu phổ biến.
