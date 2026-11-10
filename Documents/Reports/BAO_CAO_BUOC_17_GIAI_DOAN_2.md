# Giai đoạn 2 — Bước 17: Conditional AI Translation offline

**Kết luận: `OFFLINE_ACCEPTED — TRANSLATION_PROVIDER_PENDING_APPROVAL`; `BLOCKED_FOR_LIVE`.** Chỉ có sidecar gọi bằng composition tường minh trong test; không thay RunService, API, dashboard, Firestore schema hay pipeline production. `AI_ENABLED=False`, `TranslationPolicy.enabled=False` mặc định. Không gọi Wikimedia, AI, cloud, không đọc API key; chi phí AI live bằng 0. Không coi các bản dịch tổng hợp là kết quả human evaluation hay bằng chứng chất lượng production.

## Thiết kế và schema

`app/translation/service.py` nhận keyword đã cleaning/extraction/classification, chạy detector Bước 8, eligibility, gọi trực tiếp `Engine`/registry/budget/structured audit Bước 16 và kiểm tra chất lượng trước khi trả sidecar. Provider phải là đúng lớp `FakeProvider` trong đường offline này. Sidecar giữ nguyên entity ID, observation ID, original text/fingerprint, source, project, country, access, date và source URL; provenance AI, versions, status, quality, accounting và audit reference được tách riêng. Không gán bản dịch làm keyword ID hoặc evidence. Test golden so sánh object core và score trước/sau, kể cả score null/missing.

Engine nay chọn cặp task/schema/request version: `Task.TRANSLATION` chỉ dùng `translation-v1`/`translation-request-v1`; các task khác giữ `synthetic-v1`/`synthetic-request-v1`. Validator mới yêu cầu chính xác schema, source/target language `en`/`vi`, input fingerprint, entity ID, request ID và translated text; kiểm tra JSON trùng key/field thừa/thiếu, kiểu, kích thước, Unicode, refusal/truncation và binding. Mọi SUCCESS vẫn validate; không sửa output sai. Các test Bước 16 không bị sửa. Instruction template được code chọn, binding là JSON tách khỏi text nguồn; không cấp tool, shell, URL fetch hay Firestore cho provider. Source prompt injection không có quyền thay policy/limit.

## Routing, privacy và trạng thái

Detector `lexical-hints-v0.1` gợi ý en/vi theo function words, không có confidence số; còn lại `unknown`, kể cả title ngắn, tên thương hiệu, emoji, mixed hay ngôn ngữ chưa hỗ trợ. NFC chỉ dùng so sánh/detection; original NFD được giữ nguyên. NLP chỉ yêu cầu target English; UI chỉ target Vietnamese; không tự dịch tất cả. Đích phải thuộc `targets-v1`, khác source. Không lấy target hoặc output để suy ra source.

Để gọi fake, trusted caller phải bật policy, cung cấp mục đích, permission/license version được duyệt, xác nhận PII đã được kiểm tra, categories được phép và trusted tenant/job/budget. Kiểm tra thêm mẫu email/phone/key, URL hoặc instruction nguy hiểm; từ chối trước call. Đó là blocklist bổ sung, không chứng minh phát hiện hết PII. Không gửi metadata của run hoặc raw observations cho provider: chỉ gửi title và binding theo schema. Với production cần authentication/tenant membership và một policy cấp quyền, dữ liệu, license và PII đáng tin cậy; các boolean test không phải bằng chứng được phép gửi dữ liệu thật.

- `SKIPPED`: không có nhu cầu, disabled, thiếu ngôn ngữ/quyền/PII assurance hoặc input bị chặn; không gọi provider.
- `TRANSLATION_UNAVAILABLE`: provider, budget, cancellation, timeout hay validation không cho bản dịch usable; display fallback giữ original.
- `REVIEW_REQUIRED`: candidate có quality flags; không đưa candidate vào `translated_text`/display. Candidate chứa dấu hiệu dữ liệu nhạy cảm bị loại khỏi sidecar.
- `TRANSLATED`: các heuristic đã pass; `SEMANTICS_UNVERIFIED` và `NOT_REVIEWED` vẫn ghi rõ chưa chứng minh nghĩa tương đương hoặc duyệt người thật. Đây không phải bảo đảm chất lượng production.

Quality guard đối chiếu theo thứ tự placeholders `{count}`, `{{name}}`, printf, URL/ID và số/đơn vị; kiểm tra acronym, tên viết hoa hoặc tên được trusted policy bảo vệ, copy nguyên, length ratio, HTML/instruction và target language. Target detector không xác nhận được cũng chuyển review. Regex không chứng minh mọi tên, ẩn dụ, hallucination, bỏ ý hay tương đương ngữ nghĩa; thêm/bớt số hoặc unit được phát hiện nhưng nội dung bịa không chứa dấu hiệu đó có thể lọt. Cần human evaluation và policy review trước live.

## Budget, replay và cache

Engine reserve trước mỗi call, retry bounded theo fake clock/Retry-After, giữ reservation khi usage/ack không rõ và không retry permanent/schema failure. Cost chưa có bảng giá là `COST_UNKNOWN`; fake token không phải usage provider thực. Request identity ràng buộc entity, observation, source/scope/date, purpose, target và policy/permission/quality/normalization version; engine thêm fingerprint/model/schema. Cùng request ID và context replay kết quả, input thay đổi cùng ID bị từ chối; service mới dùng lại State không reset account. Việc thay đổi danh tính/version có chủ đích là request khác, vẫn chịu cùng account.

Translation cache **DISABLED**, kể cả khi synthetic cache Bước 16 được bật; không có cache production, không có TTL/eviction translation cần nghiệm thu. Engine idempotency giữ response trong fake state chỉ để thử replay; mọi lần replay vẫn qua eligibility và quality guard, không phải cache có thể dùng cho dữ liệu thật. Audit giữ hash identifier, outcome và usage/accounting, không chứa title, translation hay exception thô. Các hash không phải anonymization tuyệt đối.

## Ma trận test và kết quả

Tất cả integration test sau thuộc `Backend/tests/test_translation_offline.py`, dùng golden source fixture synthetic → cleaning/extraction/classification/scoring hiện có → detector → policy → Fake AI Engine → validator → quality → sidecar. Các mẫu dịch là synthetic fixture tự đặt, không lấy từ dịch vụ online.

| Tình huống | Test |
|---|---|
| En→Vi và Vi→En, giữ original/ID/provenance/score/null | `test_english_to_vietnamese_preserves_golden_pipeline`, `test_vietnamese_to_english`, `test_unicode_and_identity_not_merged` |
| Disabled, already target, unknown/mixed, brand/emoji, PII/URL/key/instruction | `test_default_disabled_unknown_same_language_and_sensitive`, `test_permission_policy_tenant_and_budget_fail_closed` |
| Sai schema/pair/entity/fingerprint, malformed, extra/duplicate/Unicode/refusal/truncated | `test_translation_schema_binds_request_and_rejects_unknown_fields`, `test_invalid_response_binding_and_schema`, `test_schema_task_allowlist_and_strict_edge_cases` |
| Placeholders, số/unit, entity, hallucinated số, HTML, target không xác nhận | `test_quality_placeholders_numbers_entities_and_hallucination`, `test_protected_template_tokens_and_wrong_target`, `test_unrecognized_target_language_requires_review` |
| Retry 429/500, timeout, budget, cancellation, unknown ack/usage, replay | `test_retry_timeout_replay_and_no_secret_audit`, `test_timeout_unknown_ack_cancellation_budget_and_replay`, `test_missing_usage_and_permanent_or_malformed_no_retry` |
| Tenant, permission trước replay, changed source và cache disabled | `test_cross_tenant_and_cache_disabled_even_with_engine_cache`, `test_permission_after_success_is_checked_before_idempotent_replay`, `test_original_changed_cannot_reuse_old_result` |
| Fake-only provider và output nhạy cảm không log/expose | `test_provider_not_fake_is_never_called`, `test_sensitive_provider_output_not_exposed_or_logged` |

19 test Bước 16 tiếp tục pass; cache expiry/invalidation của translation không áp dụng vì cache bị vô hiệu hóa. Golden Expected không thay đổi. Kiểm thử cuối qua seccomp chặn `socket`, `connect`, `io_uring_setup`, đóng fd kế thừa; đã kiểm tra Python socket, native socket và child đều bị EPERM. **305/305 test pass**; Golden 7/7 với từng seed 1, 983; `pip check`, `bash -n setup.sh start.sh` và `node --check Frontend/public/app.js` pass. Blocker chỉ là cơ chế offline tại máy thử, không phải kiểm soát mạng production.

## PENDING_APPROVAL, rollback và live blockers

Chưa phê duyệt provider/model thật, điều khoản/license, data sharing/retention, target coverage, chất lượng, secret management hay budget/cost. Cần schema và adapter thực, IAM, tenant auth, PII controls, đánh giá ngôn ngữ/ngữ nghĩa bởi người, quota bền vững và kiểm thử live được phép riêng. Fake state, đồng hồ, concurrency và usage chỉ chứng minh trong một process, không chứng minh distributed safety hoặc cost thật. Không có deployment hoặc migration; rollback offline bằng bỏ lời gọi sidecar và giữ `enabled=False`, bảo toàn pipeline gốc. **`BLOCKED_FOR_LIVE` giữ nguyên.**
