# D6 Vercel Fetch All — báo cáo sửa lỗi

**Ngày:** 2026-11-19
**Nhánh làm việc:** `main`
**Trạng thái:** G1–G6 PASS.
**Tiến độ:** 100% theo trọng số gate.

## Phạm vi và giới hạn

Sửa tính bền vững của Fetch dùng Firestore, khôi phục job Vercel bị kẹt, và UX sau reload. Không gửi POST Fetch, không gọi API nhà cung cấp thật, không reset dữ liệu/quota Production, không sửa IAM/billing.

**Production deployment đã xảy ra:** ban đầu, theo yêu cầu dùng một link chính, Preview `dpl_6WFCatRwF5ZByMeMGJ76Dgb4kcPw` được redeploy qua Vercel API thành `dpl_7b6cFCmW4qXCJgNuEe4Wquw6XzYV`. Metadata ghi `source: redeploy`, không có Git commit SHA; source lúc đó là worktree local tải lên Vercel. Sau đó D6 được commit/push thẳng lên `main` (`6f374e5`); workflow timeout được cập nhật ở `36a380e`. Deployment dùng khi Owner nghiệm thu là Git deployment `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F` từ commit code `36a380e`. Report-only commit `83a613e` sau đó cũng được Vercel tự build thành deployment Git Production `dpl_FQFycNry5azPUTB8aXCR3nyU6yHE` ở trạng thái `READY`; không có thay đổi app code trong commit đó. Alias Owner vẫn chỉ có một URL.

Run đã được báo cáo: `14e4f63bc0944b65b573d0b23a787c8e`. Vercel vẫn trả thông tin trạng thái từ shared state, nhưng runtime logs của run này không còn trong thời hạn lưu hiện tại. Vì vậy không thể xác nhận duy nhất lỗi nào đã xảy ra trong request Production cụ thể. Các nguyên nhân bên dưới là failure mode được tái hiện độc lập trên code hiện tại, không phải kết luận về log đã hết hạn.

## Phát hiện và thay đổi

1. **Checkpoint commit có kết quả không rõ:** Firestore có thể commit transaction nhưng client mất acknowledgement. `SourceCheckpointJournal.save()` nay thử lại cùng snapshot, revision và digest tối đa ba lần. Nếu lần đầu đã commit, kiểm tra revision+digest nhận diện bản đã lưu; không gọi lại provider.
2. **Giới hạn snapshot không khớp:** checkpoint cho phép payload đã giải quyết đến 5.000.000 byte nhưng blob adapter giới hạn 4.000.016 byte. Giới hạn blob được nâng lên 5.000.000 byte và test round-trip fixture 4,1 MB qua nhiều chunk. Theo tài liệu Firestore chính thức đã kiểm tra ngày 2026-11-19, mỗi document tối đa 1 MiB và API request tối đa 10 MiB; chunk hiện tại 196.608 byte giữ từng document dưới giới hạn. Việc kiểm tra tài liệu không xác nhận project credentials hay request Production cụ thể. [Firestore quotas and limits](https://docs.cloud.google.com/firestore/quotas)
3. **Job `RUNNING` hết lease:** đọc status/history nay sửa lease hết hạn thành `QUEUED` kèm `recovery_available`, hoặc đặt trạng thái terminal khi hết deadline/số lần thử. GET không dispatch worker. Nếu còn kết quả nguồn đã checkpoint, job kết thúc `PARTIAL` để giữ phần đã lưu. Checkpoint giữ theo hạn retention riêng thay vì bị cắt theo deadline thực thi job.
4. **Reload và tiếp tục có kiểm soát:** dashboard render capabilities gần nhất từ cache trình duyệt, ghi rõ trạng thái cũ và cho thử lại khi request lỗi. Cache không chứa Fetch ID, kết quả, credential hay dữ liệu riêng. Reload chỉ GET. Run History có nút **Tiếp tục Fetch** cho job đã phục hồi; thao tác POST chỉ xảy ra khi người dùng bấm, gửi lại scope nguồn trong lịch sử để server nhập lại job queued và tái sử dụng checkpoint.
5. **Phạm vi lịch sử khôi phục:** shared history nay giữ `request_kind` và `history_days`, ngoài danh sách nguồn, để UI tiếp tục đúng loại job và cửa sổ lịch sử.

## Kiểm tra

- **150 backend tests PASS** khi chạy lại trên Python của `Backend/.venv`: 119 test qua `test_d5_source_checkpoints`, `test_d5_public_admission`, `test_d5_distributed_worker`, `test_d5_firestore_shared_cache`, `test_google_trends`, `test_bing_handler`, `test_vercel_shared_live_fetch`; thêm 31 test qua `test_d5_guarded_attempt`, `test_d5_live_gateway`, `test_d5_firestore_atomic_state`.
- **26 Firestore Emulator integration tests PASS** trên GitHub Actions run [44542548880](https://github.com/trandaiquoc/TrendScout/actions/runs/44542548880), commit `36a380ed90ce0ca3408b8ae23ad6d3e620cddf59`. Chạy `test_firestore_transactional` và `test_firestore_shared_runtime`; gồm Google Fetch route với RSS fixture, cache/quota/checkpoint persistence, nhiều client và lost-commit retry. Không gọi Google/provider thật.
- **24 frontend tests PASS** qua `fetch-loading-state`, `fetch-source-selection` và `run-history-coverage`.
- **6 browser modules** được kiểm tra cú pháp dưới ES modules.
- `Backend/scripts/prepare_vercel.py` chạy thành công. Bản chạy lại không in số lượng assets.
- Vercel Preview `https://trend-scout-np8wty5kt-rakoon1.vercel.app` ở trạng thái `READY`, `target: null`; build dùng cây làm việc hiện tại, không push hoặc tạo nhánh. GET tĩnh `/dashboard/` và các module `fetch.js`, `run-history.js` đều trả **HTTP 200**; HTML có `lang="vi"`, module Run History có trường và nhãn recovery.
- Playwright render Preview bằng share link tạm, với mọi `/api/**` bị chặn ngay trong browser và trả mock `503`. Shell tiếng Việt và nút thử lại hiển thị; không có request app API tới server. Ở viewport 390 px, `scrollWidth = clientWidth = 390`, không tràn ngang. 7 console errors đều là HTTP 503 do mock có chủ ý.
- URL Owner cung cấp `https://trend-scout-mu.vercel.app` ban đầu là alias **Production** ở commit cũ `b57389babc6ea308043a31c9ef66a935da3b269d` (deployment `dpl_2r294JjWURedcsGhwiaAB8zDDbmZ`). Theo chỉ dẫn của Owner dùng một link, bản D6 đã được phát hành lên alias đó.
- Production deployment dùng khi Owner nghiệm thu là `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F`, `target: production`, Git build từ commit code `36a380e`. Vercel web fetch xác nhận HTML và module `fetch.js`, `run-history.js` trên URL Owner đưa trả **HTTP 200**; module Run History có recovery field/label. Đây là kiểm tra tĩnh, không xác nhận UI chạy cùng API thật.
- Owner manual acceptance **PASS** trên URL Production duy nhất `https://trend-scout-mu.vercel.app/dashboard/`: reload trang, dashboard tiếng Việt hiển thị, không kẹt ở “Đang kiểm tra”, và Run History tải dòng job đã lưu với bốn nguồn. Production GET `/api/v1/runs/history?limit=20` trả **HTTP 200**, có run `14e4f63bc0944b65b573d0b23a787c8e`, trạng thái `PARTIAL`, đủ Bing, Google, Wikimedia và YouTube; response báo `history_source=local_sqlite_and_shared_firestore`. Đây là bản ghi cũ đã lưu, không phải kết quả của Fetch mới. `manual_history_status=LOCAL_LEDGER_ONLY` chỉ áp dụng lịch sử `MANUAL_ANALYSIS`; mục Fetch vẫn có trong Run History.
- Truy vấn runtime logs mới nhất bị Vercel API từ chối `403`; tình trạng log runtime hiện **chưa xác minh**, không kết luận rằng không có lỗi.
- Playwright mở trực tiếp link Production bị chuyển tới trang đăng nhập Vercel. Owner đã kiểm tra UI trong phiên đăng nhập của mình. Không tắt Deployment Protection hay thay đổi quyền project.
- JavaScript dashboard tự gửi GET capabilities/history khi chạy bình thường. Trong Playwright Preview smoke, mọi route `/api/**` bị chặn tại browser và mock 503 nên browser smoke không gửi request app API thật. Riêng QA Production gọi GET history chỉ đọc và nhận HTTP 200; không gửi Fetch POST hay gọi provider.
- `git diff --check` sạch. Các kiểm tra local dùng fixture/transport giả và SQLite tạm; Firestore Emulator được chạy trong runner GitHub Linux cô lập với `bwrap` và network chỉ-loopback vì Windows 1 không có Java/WSL.
- GitHub Actions Firestore Emulator run `44542548880` và Pages build run `44542547219` đều **success** trên commit `36a380ed90ce0ca3408b8ae23ad6d3e620cddf59`.
- Code D6 và các cập nhật workflow đã được push trực tiếp lên `main`, không tạo nhánh công việc. Các commit sau nghiệm thu chỉ cập nhật báo cáo; Vercel Git tự build commit trên `main`.

## Gate

| Gate | Trọng số | Trạng thái | Bằng chứng |
|---|---:|---|---|
| G1 — tái hiện và khoanh vùng | 15% | PASS | Bốn failure mode được tái hiện bằng test trước khi sửa. Nguyên nhân của run Production cũ vẫn chưa xác minh được vì log hết hạn. |
| G2 — persistence/Firestore | 25% | PASS | Retry checkpoint idempotent, chunk snapshot 4,1 MB, retention độc lập với deadline. |
| G3 — lifecycle/recovery | 20% | PASS | Lease hết hạn, deadline, checkpoint đã hoàn tất, status/history không dispatch, POST tường minh để tiếp tục. |
| G4 — frontend/reload UX | 15% | PASS | Capabilities cache có nhãn stale; Run History tiếp tục theo scope lưu; reload không tự POST. |
| G5 — tích hợp/hồi quy | 15% | PASS | 150 backend, 24 frontend và 26 Firestore Emulator tests đạt; GitHub Actions run trên commit `36a380e` success. |
| G6 — Vercel QA + Owner acceptance | 10% | PASS | Production deployment `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F` từ commit `36a380e` `READY`; API history trả 200 và Owner xác nhận UI/history PASS sau reload trên alias chính. Không gửi Fetch mới hay gọi provider thật. |

## Kết luận nghiệm thu

G1–G6 đều PASS. Vercel Production chạy code đã push lên `main`; Owner xác nhận dashboard và Run History sau reload. Không gửi Fetch POST, gọi provider thật, reset Production data/quota hay thay đổi IAM/billing trong quá trình QA. Bản ghi Production hiện có mang trạng thái `PARTIAL`; việc hiển thị đúng trạng thái đã lưu được nghiệm thu, còn một lần Fetch live mới chưa được chạy.

## Production deployment and rollback

- The Production deployment used for Owner acceptance was `dpl_GtB6SWCR6Qfint8qLuyCLpzaVh5F`, built by Git from app-code commit `36a380e` and assigned to the single Owner-provided alias. Report-only commit `83a613e` was later auto-deployed by Vercel as `dpl_FQFycNry5azPUTB8aXCR3nyU6yHE`, `READY`. No Fetch POST or provider request was sent during deployment verification.
- Rollback target is previous Git-backed Production deployment `dpl_8KHV54j2qC74eUQCmzJAHKsDEroS` at D6 code commit `6f374e5aa8cc5a3c0eb64ef810cad82432429652`. If a release issue appears, use Vercel `request_rollback` for project `prj_dB1JIG2hJG8NUukGPAaqvyI1Pk3R` with that deployment ID, then verify `trend-scout-mu.vercel.app` points back to it. No rollback request was sent.
