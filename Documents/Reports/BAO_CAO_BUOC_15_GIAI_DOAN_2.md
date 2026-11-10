# TrendScout — Giai đoạn 2, Bước 15: Retention & Cleanup Safety

**Kết luận: `OFFLINE_ACCEPTED — POLICY_PENDING_APPROVAL`; `BLOCKED_FOR_LIVE`.** Kết quả chỉ áp dụng fake repository, dữ liệu tổng hợp và kiểm thử trong một tiến trình. Không có Firestore delete adapter hoặc endpoint cleanup; không bật TTL, scheduler, Cloud Functions, billing hay deploy. Không đọc hoặc xóa run cloud `30e96cf96abe48dbb304fb9808c89873` trong bước này.

## Phạm vi và chính sách

`Backend/app/retention/offline.py` mô phỏng hai policy version `simulation-v1`: `STRICT_RETENTION` với ngưỡng giả định 7 ngày và `GRACE_FALLBACK` thêm tối đa 7 ngày cho active khi có evidence refresh thất bại. Khi thiếu evidence, active được bảo vệ và báo `ACTIVE_GRACE_UNVERIFIED`; khi có evidence, dry-run báo `STALE`. Strict, legal deletion hoặc hết grace có thể chuyển pointer active sang unavailable trong fake transaction trước khi xóa. **Không có giá trị thời hạn hoặc policy production nào được phê duyệt.**

`DATA_CLASS_POLICY` phân biệt results, raw, source snapshots, baseline, quality reports, job metadata, audit, tenant data và saved research. Cleanup dataset không xóa job, audit, settings hay user research; baseline/shared reference còn hiệu lực chặn cleanup. License không rõ mặc định `LICENSE_UNKNOWN`. Legal deletion hoặc license raw hết hạn mâu thuẫn với hold/reference trả `POLICY_CONFLICT`; không tự chọn giải pháp cho xung đột.

Kịch bản fake `license_raw_due` chỉ áp dụng khi toàn bộ contract nguồn và metadata được nhận diện. Shard score còn chứa `current_views_ceil`, baseline và article gốc, nên bước này **chiếu shard score** về bốn trường dẫn xuất `keyword_id`, `score`, `status`, `algorithm_version` dưới action `REDACT` đã khai báo; sau đó xóa raw observations và snapshots vì snapshot có thể chứa rejected raw records. Dataset chuyển `PARTIALLY_DELETED`, không thể trở lại active. Receipt và digest của projection cho phép kiểm tra phần còn lại trước plan sau. Unknown shard/metadata/quality contract bị từ chối. Cờ `license_raw_due` được test tiêm tường minh; prototype không tự tra license hay tính thời hạn theo nguồn. Đây là mô phỏng chính sách dữ liệu, không khẳng định về điều khoản Wikimedia hay phê duyệt redaction thực tế. Nếu quota, hold hoặc crash chặn giữa chừng, raw có thể còn tồn tại và phải được xử lý theo quy trình pháp lý được duyệt trước live.

## Allowlist, plan và trạng thái

Chỉ fake `FakeDatasetStore` được chấp nhận; không có cấu hình environment để bật xóa thật. `DRY_RUN=True`, `LIVE_DELETE_ENABLED=False`; `start` mặc định dry-run và chỉ nhận boolean `execute_fake=True` do test tiêm trực tiếp. Plan cho biết operator approval vẫn bắt buộc trước một triển khai tương lai.

Namespace đóng là `analysis_datasets/{dataset_id}` và các document con `shards/{partition}` cùng `quality/report`. Ba partition được nhận diện là `raw-observations-v1`, `snapshots-v1`, `scores-v1`; ID phải canonical và không chứa path separator. Cleanup inventory manifest `cleanup-inventory-v1` liệt kê toàn bộ document, gắn fingerprint strict manifest Step 14; plan chứa paths/actions, identity, version, snapshot references, pointer revision/fence, estimate, quota, deadline và audit reference. Các partition, metadata, subcollection hoặc tenant scope lạ fail closed. Không dùng prefix tùy ý; không đụng Firestore root, legacy demo, users, settings, credentials, billing hoặc saved research.

Eligibility tính tuổi từ thời điểm `PUBLISHED` trong fake audit (không dùng thời gian tạo job), đối chiếu manifest/quality/digest, job identity và publication receipt; kiểm tra expiry, trạng thái, active, hold, shared/baseline references, legal/license và scope. Dataset `READY` chờ publish, draft chưa có manifest hoặc dữ liệu mơ hồ không được xóa. Dataset rỗng đã verify chỉ đủ điều kiện theo policy giả định sau expiry; draft rỗng chưa verify bị từ chối.

Lifecycle fake ghi history `PLANNED → VALIDATING → READY → DELETING → COMPLETED`, cùng `PAUSED`, `RETRY_WAIT`, `BLOCKED`, `FAILED` và `PARTIALLY_DELETED`. Full dataset chỉ `COMPLETED` sau xác nhận mọi child và xóa parent cuối cùng; xóa raw một phần không được báo full completion. Cancel giữ dataset không khả dụng và checkpoint; không khôi phục pointer đến dữ liệu đã xóa một phần.

## Atomicity, quota và recovery

Reservation, pointer CAS, tombstone `CLEANING` và fake publisher dùng cùng RLock. Sau reservation, fake publisher không thể publish dataset đang dọn; mỗi batch đối chiếu lại pointer revision, resource owner/token, manifest, job identity, reference version, shard còn lại và quality. Thay đổi sau plan hoặc xuất hiện unknown child làm fail closed; trước xóa parent có revalidation bổ sung. Hai thứ tự publish/cleanup và race threads đều được test. Đây không phải Firestore transaction hoặc multi-host fencing.

Thứ tự path ổn định, parent cuối. `Limits` giới hạn tổng estimated reads/writes/deletes, batch tối đa 25, retry tối đa 5 và deadline một giờ giả định; `step` còn giới hạn mỗi invocation. Estimates bao gồm planning/eligibility/reference, revalidation, checkpoint/audit, redaction/delete và retry overhead; chúng không phải billing Firestore. Quota hết dừng `PAUSED` với checkpoint, không tự tăng hoặc reset budget. Nếu hết **giới hạn tổng** hoặc deadline, prototype không tự tạo budget mới hay tái kích hoạt dataset: cần quy trình đối soát và phê duyệt riêng trước xử lý tiếp. Pause/cancel cũng giữ tombstone an toàn.

Mỗi action fake cập nhật receipt/cursor/audit dưới cùng lock với mutation. Fake checkpoint chứa plan ID, dataset ID, cursor, completed paths, last path, redaction digest, retries, error và counters. Sau crash, state snapshot/reload giữ receipt, retry có backoff và giới hạn; replay checkpoint cũ không tiến thêm batch. Test pickle dùng file tạm và dữ liệu tin cậy; không dùng pickle cho input không tin cậy. Không coi giả lập này là disk durability.

## Test matrix

Tất cả tên dưới đây thuộc `Backend/tests/test_retention_offline.py`, trừ khi ghi khác. Các test thực thi qua fake store và orchestrator, không chỉ helper.

| # | Trường hợp | Bằng chứng |
|---|---|---|
| 1 | Dry-run không mutate | `test_constructing_dry_run_does_not_mutate_fake_store`, `test_default_start_is_dry_run`, `test_enumeration_is_dry_and_marks_invalid_dataset` |
| 2 | Chưa hết hạn | `test_nonexpired_and_grace_expiry` |
| 3–5 | Active, strict unavailable, grace | `test_dry_run_keeps_active_and_all_data`, `test_strict_fake_execution_unpoints_active_before_deleting`, `test_grace_requires_recorded_refresh_failure`, `test_nonexpired_and_grace_expiry` |
| 6 | Legal, license, hold | `test_license_unknown_and_legal_hold_conflict_fail_closed`, `test_legal_delete_overrides_grace_when_no_hold`, `test_license_raw_expiry_preserves_derived_and_supports_later_cleanup` |
| 7–8 | Publish/pointer đổi, plan stale | `test_publish_and_cleanup_both_deterministic_orders`, `test_pointer_change_during_execution_stops_before_next_delete`, `test_protection_added_after_planning_invalidates_plan`, `test_old_job_cannot_expire_a_just_published_dataset` |
| 9–11 | Sai ID, traversal/prefix, unknown child | `test_traversal_wrong_identity_and_unknown_subcollection_fail_closed`, `test_forged_execution_plan_cannot_complete_with_children`, `test_unknown_subcollection_injected_immediately_before_parent_is_preserved` |
| 12–13 | Thiếu/sửa manifest | `test_missing_or_tampered_manifest_and_partial_draft_fail_closed`, `test_changed_authoritative_job_identity_rejects_cleanup` |
| 14–15 | Child đầy đủ, parent cuối | `test_parent_is_last_and_job_and_audit_are_preserved`, `test_crash_matrix_reload_preserves_receipts_and_does_not_repeat_deletes` |
| 16 | Quota/counters/checkpoint | `test_budget_cutoffs_never_exceed_limits_or_complete_early`, `test_quota_stops_after_bounded_deletes`, `test_start_quota_includes_reservation_audit_and_pointer_write` |
| 17–18 | Crash/reload/replay | `test_crash_matrix_reload_preserves_receipts_and_does_not_repeat_deletes`, `test_replay_of_old_checkpoint_does_not_advance_another_batch`, `test_repeated_crash_before_batch_obeys_retry_budget` |
| 19–21 | Cancel, stale worker, race | `test_cancellation_stops_cleanup_without_reactivating_partial_data`, `test_old_worker_cannot_modify_reserved_dataset`, `test_publish_cleanup_race_has_no_dangling_pointer`, `test_two_cleanup_workers_with_same_cursor_delete_once` |
| 22–23 | Shared và baseline | `test_shared_references_from_multiple_tenants_are_independent`, `test_hold_shared_and_baseline_references_block_cleanup`, `test_hold_during_cleanup_stops_next_batch` |
| 24 | Tenant/user/settings/secrets | `test_unrecognized_tenant_scope_cannot_enter_cleanup`, `test_legacy_demo_and_other_namespaces_are_not_cleanup_targets` |
| 25 | Empty và partial draft | `test_verified_empty_dataset_has_explicit_expired_policy`, `test_missing_or_tampered_manifest_and_partial_draft_fail_closed` |
| 26 | Không trỏ vào dataset xóa dở | `test_publish_cleanup_race_has_no_dangling_pointer`, `test_license_raw_expiry_preserves_derived_and_supports_later_cleanup` |
| 27 | Legacy demo | `test_legacy_demo_and_other_namespaces_are_not_cleanup_targets` |
| 28 | Không network/cloud | Fake-only type guard, MockTransport/fixture; toàn bộ test chạy dưới seccomp chặn tạo socket và connect. Không có cloud delete. |

Fault injection bao gồm trước/sau planning, sau validation, trước batch, sau delete/redaction và quanh receipt/checkpoint, giữa các page, trước/sau parent và sau commit trước acknowledgement. Race cũng được thử theo hai thứ tự xác định, không dựa vào một kết quả thread duy nhất.

## Regression và giới hạn live

Kết quả: **267/267 tests pass**, gồm 47 test Bước 15; Golden Regression **7/7** với từng `PYTHONHASHSEED=1` và `983`; `pip check`, `bash -n setup.sh start.sh`, `node --check Frontend/public/app.js` pass. Tests và các lệnh kiểm tra trên chạy dưới seccomp từ chối syscall `socket`, `connect`, `io_uring_setup`, đóng inherited descriptors và áp dụng cho child process; đã xác nhận riêng Python/libc socket, connect và child process bị chặn. Không sử dụng network namespace hay tuyên bố kiểm thử cloud. `git diff --check` pass.

So với baseline `a62b708`, API, Firestore demo schema/storage, manual-run, HostRunLock, Vercel read-only, frontend, scoring/cleaning/classification v0.1 và Golden Expected không được thay đổi. Fake store chỉ thêm khởi tạo retention state; scheduler/worker production vẫn không được wire.

**Rủi ro và điều kiện trước live:** fake deletion không thể hoàn tác sau khi commit; pointer unavailable không phục hồi các child đã mất. Cần phê duyệt retention cho từng lớp dữ liệu, semantics legal/license, projection, baseline, grace, thời hạn audit, tenant references và operator approval; thiết kế recovery khi quota/deadline hết, backup/restore và rollback; triển khai transaction adapter có fencing/CAS, emulator concurrency và crash tests; xác nhận IAM, hosting, trusted clock, quota/cost và quan sát vận hành. Chưa có bằng chứng cho các điều kiện này nên **`BLOCKED_FOR_LIVE`** giữ nguyên. Không triển khai Bước 16 hoặc bước sau.
