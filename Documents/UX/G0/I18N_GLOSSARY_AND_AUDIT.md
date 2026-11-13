# VI/EN localization audit and glossary

**Audit date:** 2026-11-13 · **Default:** Vietnamese. Audit compared locale keys and reviewed static UI/status patterns; no code changed.

## Findings

- `Frontend/public/modules/i18n/vi.js` and `en.js` have equal key sets: **614 keys each; no key-set mismatch**.
- Vietnamese/light are defaults; preferences are read from local storage. Current Windows acceptance reports both languages and light/dark exercised on key flows.
- Technical literals remain in some UI/API states, including `NOT_MEASURED`, `NOT_AVAILABLE`, `COST_UNKNOWN`, `UNCONFIGURED`, `DISABLED`, `NOT_YET_EVALUATED`, `LOCAL_LIVE_VERIFIED`, `PARTIAL`, and `SKIPPED`. Map known codes to short VI/EN labels; keep raw codes in expanded technical details.
- `index.html` contains English page metadata/title and some fixed provider names/status values. Sources and chart elements include hard-coded labels/units. Audit `aria-label`, placeholders, dialog/error/loading/empty copy, tooltips, date/number formatting and dynamic strings—not only locale key parity.
- Equal catalogs do not prove translation quality or completeness. Windows acceptance verifies key flows but not every rendered phrase. This G0 static audit did not inspect every UI state in-browser.

## AI and Product Research terminology

Current local classification uses CPU FastEmbed 0.9.1 with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`; this embedding path is local and is used for Collection matching/classification. The Settings panel describes an LLM/provider integration as disabled/unconfigured. No cloud LLM provider or paid AI usage is evidenced. UI should say “Local embeddings” for the active path and “LLM provider: not configured” for the disabled provider panel. Product Research is an unverified future workflow, not a measured AI output.

## Glossary

| English/source status | Vietnamese | Guidance |
| --- | --- | --- |
| Collection | Bộ sưu tập | Product noun |
| Keyword | Từ khóa | Keep original spelling in detail |
| Unclassified | Chưa phân loại | Inbox/filter; not impossible to classify |
| Classified | Đã phân loại | Show taxonomy path/evidence |
| Suggested classification | Đề xuất phân loại | Proposal, not confirmed fact |
| Ambiguous | Chưa rõ (bằng chứng chưa thống nhất) | Not classifier failure |
| Unknown / insufficient context | Chưa đủ ngữ cảnh | Retry only when more evidence is available |
| Not in taxonomy | Chưa có nhóm phù hợp | Distinct from missing context |
| Needs review | Cần xem xét | Human action under current mode |
| Insufficient data | Chưa đủ dữ liệu để đánh giá | Not zero |
| Partial results | Kết quả một phần | Give returned/expected scope and reason |
| Cached data | Dữ liệu đã lưu | Include original observation/fetch time and age |
| Live data | Dữ liệu trực tiếp | Only when this operation used live provider response |
| Stale data | Dữ liệu đã cũ | State age and last success |
| Not applicable | Không áp dụng | Include compatibility reason |
| Skipped | Đã bỏ qua | Explain why; distinct from failed |
| Failed | Thất bại | Preserve safe actionable diagnostic |
| Observed popularity | Mức độ phổ biến quan sát được | Descriptive, not growth/demand |
| Trend growth | Mức tăng theo thời gian | Requires comparable baseline |
| Censored top list | Danh sách top bị giới hạn | Absence does not mean zero |
| Approximate pageviews | Lượt xem trang ước tính | Not unique people or purchase intent |
| Result rank | Thứ hạng kết quả | Ordinal/query-scoped, not volume |
| Market/scope | Phạm vi thị trường/dữ liệu | Explain filter and limits |
| Current runtime health | Tình trạng hệ thống hiện tại | Separate from historic verification |
| Production readiness | Mức sẵn sàng cho production | Separate from local LIVE test |
| Local embeddings | Mô hình embedding chạy cục bộ | Not a cloud LLM |
| LLM provider | Nhà cung cấp mô hình ngôn ngữ | Say not configured when absent |

## Message and formatting rules

- Use locale-aware `Intl.NumberFormat` and `Intl.DateTimeFormat`; include timezone for times and preserve exact source observation date.
- Keep official provider/API names; translate surrounding explanation.
- Avoid unexplained acronyms and raw enum labels in primary UI. Technical detail can show code plus translated meaning.
- Errors state what happened, whether saved data is available and a safe next step. Never show credentials or secret-bearing URLs.
- Localize aria labels, announcements, tab semantics and chart alternatives equally.

## Suggested G1 validation

Automated: key parity, nonblank values, no untranslated status fallback. Manual: VI/EN review of all routes/states, date/number formatting, keyboard/screen reader labels and mobile. These are future criteria, not tests run in G0.
