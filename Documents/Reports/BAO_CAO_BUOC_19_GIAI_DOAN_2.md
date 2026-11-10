# Giai đoạn 2 — Bước 19: Product Research offline

**Kết luận: `OFFLINE_ACCEPTED — RESEARCH_POLICY_PENDING_APPROVAL`; `BLOCKED_FOR_LIVE`.** Module chỉ hoạt động trong test synthetic với fake repository; không nối RunService, API, dashboard hoặc Firestore. Không gọi Wikimedia, marketplace, AI hay cloud, không đọc API key; chi phí AI live bằng 0. Không có nguồn commercial thật hoặc human review: **`REAL_WORLD_RESEARCH_QUALITY_NOT_EVALUATED`**.

## Ranh giới và contract

`app/research/offline.py` có `Case`, `Relevance`, `Evidence`, `Claim`, `Verification`, `Opportunity`, `Idea`, `Risk`, `Summary` và `FakeResearchRepository`. `ResearchService` tạo case từ keyword/trend v0.1 cùng identity, scope và window; không sửa title, source, ID, rule, views, missing, score hoặc ranking. Case có tenant, dataset, thời gian, policy, taxonomy và classification version. Khác tenant/source/window không tự merge. Không có Firestore schema mới.

Attention là tham chiếu score `wikimedia-trend-v0.1`, không được chuyển thành demand, sales, conversion hay giá. Khi không có commercial evidence hợp lệ, relevance `UNKNOWN`, demand `UNKNOWN`, opportunity `None` / `INSUFFICIENT_COMMERCIAL_EVIDENCE`. Product existence synthetic chỉ cho `POSSIBLY_RELEVANT`; tín hiệu thương mại synthetic yêu cầu review. Formula thử `synthetic-intent-index-v1` chỉ trả giá trị index fixture `purchase_intent_index` đã verified đúng unit `index_0_100`, source, entity, market và window. Zero hợp lệ khác missing; mode `SYNTHETIC_ONLY`. Đây là identity formula để kiểm thử contract, không phải policy normalization, dự báo hay điểm cơ hội thực tế tại Mỹ.

## Evidence và claim

Registry chỉ nhận fixture synthetic từ source/type/metric/unit được allowlist. Evidence chứa ID, claim/entity/idea binding, nguồn và fixture reference, timestamps/window/market, metric/value/unit, permission/license, sensitivity, tenant/shared scope, fingerprint, content digest, verification và expiration. Reference bắt buộc `fixture:`, không tạo hoặc gọi marketplace URL. Trusted fixture registration là ranh giới giả lập: digest phát hiện sai lệch so với bản đã đăng ký, không tự chứng minh sự thật hay quyền thương mại của nguồn production.

Verifier yêu cầu evidence đã attach vào case, có thể truy cập đúng tenant, còn hạn, không tương lai, digest và permission hợp lệ, source contract chính xác, và mọi trường claim khớp subject/claim/idea/source/scope/time/metric/value/unit. Attention không xác minh sales; AI/manual hypothesis không xác minh commercial claim. Claim thiếu citation là `UNVERIFIED`, citation sai bị `REJECTED`. URL trông hợp lệ không đủ để xác minh nguồn.

## Hypothesis, risk và review

Không triển khai AI idea generation: `AI_ENABLED=False`, `ai_ideas` chỉ trả `SKIPPED/AI_DISABLED`, không nhận provider, private notes hay budget và không có đường gọi provider; retry, timeout và AI output schema của research vì thế **không áp dụng**. Test xác nhận fake provider chứa timeout/JSON lỗi vẫn không bị gọi và core không đổi. Có thể lập hypothesis thủ công khi có attention evidence được phép; text bị giới hạn, guard từ chối các mẫu giá/sales/URL/instruction rõ ràng. Regex không thể bảo đảm phát hiện mọi khẳng định sai: ý tưởng vẫn `UNVERIFIED` hoặc `REVIEW_REQUIRED`, không là product recommendation hay market fact.

Risk flags gồm trademark/brand, copyright/franchise, person/likeness, counterfeit/imitation, unsupported medical claim, restricted categories, privacy, source permission, advertising claim và IP chưa được làm rõ. Đây chỉ là tín hiệu review, không phải kết luận pháp lý. Permission chưa rõ không được tạo idea. Một số risk cao buộc `REVIEW_REQUIRED`.

`review.py` dùng fake reviewer authority Bước 18 nhưng decision type riêng: `APPROVED_FOR_RESEARCH`, `REJECTED`, `REQUEST_MORE_EVIDENCE`. Phê duyệt chỉ cho nghiên cứu tiếp, không chứng minh nhu cầu bán hàng. Decision gắn tenant, case/idea, evidence fingerprint, policy, reviewer, timestamp và reason; evidence, permission, idea, policy hoặc hết hạn làm quyết định stale. Không có reviewer thật hoặc production RBAC.

Research fake tách namespace `tenant_private_research_fake` khỏi dataset cleanup; tenant khác không đọc/ghi case/evidence, shared evidence cần explicit shared-write capability và permission. Test chạy **cleanup fake Bước 15 thực tế** xóa dataset mẫu, research fake vẫn còn. Điều này không chứng minh durability, retention/legal deletion, bảo mật multi-host hay Firestore transactions. Private research không được truyền đến AI vì không có đường AI.

## Offline E2E và ma trận

`Backend/tests/test_research_offline.py` chạy Wikimedia fixture synthetic qua adapter mock → cleaning → extraction → rule → score v0.1 → case → registry/claim → idea/risk → fake review. Dữ liệu không được dùng làm market evidence thật.

| Failure matrix | Test / chứng minh |
|---|---|
| 1–6: attention cao, unknown, missing/zero, synthetic metric, unit/scope/market | `test_golden_attention_does_not_imply_opportunity_or_demand`, `test_synthetic_commercial_score_requires_verified_metric`, `test_actual_synthetic_zero_is_not_missing`, `test_global_scope_cannot_support_us_case` |
| 7–16: số/URL bịa, citation sai, expiry, permission, entity/window/digest, idea | `test_idea_untrusted_fabrications_and_prompt_injection_rejected`, `test_claim_mutations_fail_closed`, `test_registration_rejects_fabrication_tampering_and_permission`, `test_citation_exists_but_does_not_support_sales`, `test_wrong_idea_binding_and_invented_sources_never_validate` |
| 17–20: brand, copyright, likeness, restricted/medical | `test_risk_flags_are_review_not_legal_conclusions` |
| 21–23: AI disabled, timeout/JSON lỗi/no-budget không có đường call | `test_ai_unavailable_by_design_never_calls_fake_provider`, `test_ai_timeout_invalid_output_and_no_budget_are_not_invoked_when_disabled`; không tuyên bố đã chạy timeout/schema/budget của một integration research vì integration đó không tồn tại |
| 24–29: tenant/shared, forged reviewer, stale, identity, cleanup, injection | `test_tenant_shared_permission_and_cleanup_separation`, `test_review_trusted_staleness_and_rejection`, `test_approval_stale_after_permission_idea_or_policy_change`, `test_different_tenant_scope_and_source_not_merged`, `test_actual_fake_retention_cleanup_does_not_delete_research`, `test_repository_rejects_forged_approved_and_fabricated_ideas` |
| 30–35: score null, golden, synthetic/unknown, no live | `test_golden_attention_does_not_imply_opportunity_or_demand`, `test_summary_reports_only_synthetic_completeness`, Golden và network blocker |

Summary đo completeness fixture: ví dụ một claim được verify và một thiếu evidence cho coverage 50%, unsupported count 1, needs-review/risk-flag rate 100% cho một idea. Đây không phải precision hay market validation; không có false-positive rate thực.

## Kiểm thử cuối và giới hạn

**348/348 tests pass**, không có test skip; 20 test research mới, 328 test cũ. Golden **7/7** cho từng seed 1 và 983; `pip check`, `bash -n setup.sh start.sh`, `node --check Frontend/public/app.js` pass. Tất cả chạy qua launcher seccomp chặn `socket`, `connect`, `io_uring_setup`, đóng fd kế thừa; xác minh Python socket, native socket và child bị EPERM. Launcher là công cụ test tại máy, không phải network control production. Không thay Golden Expected hoặc code scoring/classification/API/Firestore/AI/translation/enrichment/retention cũ.

PENDING_APPROVAL: thị trường/category, nguồn commercial và quyền, retention user research, policy IP/brand/person, evidence, formula opportunity thật, review/RBAC, model/provider/budget và secret management. Trước live cần nguồn và license được duyệt, xác thực tenant, durable storage/transactions, privacy, data quality/human labels, monitoring và kiểm thử live riêng. Không có hoạt động deploy/billing/IAM. Rollback bằng không gọi module sidecar, không cần migration core. **`BLOCKED_FOR_LIVE` giữ nguyên.**
