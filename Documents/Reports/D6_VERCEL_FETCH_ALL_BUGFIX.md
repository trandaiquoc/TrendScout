# D6 Vercel Fetch All — báo cáo sửa lỗi

**Ngày:** 2026-11-19
**Nhánh làm việc:** `main`
**Trạng thái:** G1–G4 PASS; G5 chờ Firestore Emulator acceptance trên `main`; G6 chờ Owner acceptance.
**Tiến độ:** 75% theo trọng số gate.

## Phạm vi và giới hạn

Sửa tính bền vững của Fetch dùng Firestore, khôi phục job Vercel bị kẹt, và UX sau reload. Không gửi POST Fetch, không gọi API nhà cung cấp thật, không reset dữ liệu/quota Production, không sửa IAM/billing.

**Production deployment đã xảy ra:** theo yêu cầu dùng một link chính, deployment `dpl_7b6cFCmW4qXCJgNuEe4Wquw6XzYV` được tạo qua Vercel API bằng cách redeploy Preview `dpl_6WFCatRwF5ZByMeMGJ76Dgb4kcPw`. Metadata của Vercel ghi `source: redeploy`, không có Git commit SHA. Preview source được tải lên từ worktree local; vì vậy Vercel chưa đọc commit D6 từ GitHub. Lúc kiểm tra gần nhất, `origin/main` vẫn ở `b57389babc6ea308043a31c9ef66a935da3b269d` và các sửa đổi D6 còn ở worktree. Việc này đang được khắc phục bằng commit/push trực tiếp trên `main`; alias dùng cho Owner vẫn là một URL.

Run đã được báo cáo: `14e4f63bc0944b65b573d0b23a787c8e`. Vercel vẫn trả thông tin trạng thái từ shared state, nhưng runtime logs của run này không còn trong thời hạn lưu hiện tại. Vì vậy không thể xác nhận duy nhất lỗi nào đã xảy ra trong request Production cụ thể. Các nguyên nhân bên dưới là failure mode được tái hiện độc lập trên code hiện tại, không phải kết luận về log đã hết hạn.

## Phát hiện và thay đổi

1. **Checkpoint commit có kết quả không rõ:** Firestore có thể commit transaction nhưng client mất acknowledgement. `SourceCheckpointJournal.save()` nay thử lại cùng snapshot, revision và digest tối đa ba lần. Nếu lần đầu đã commit, kiểm tra revision+digest nhận diện bản đã lưu; không gọi lại provider.
2. **Giới hạn snapshot không khớp:** checkpoint cho phép payload đã giải quyết đến 5.000.000 byte nhưng blob adapter giới hạn 4.000.016 byte. Giới hạn blob được nâng lên 5.000.000 byte và test round-trip fixture 4,1 MB qua nhiều chunk. Theo tài liệu Firestore chính thức đã kiểm tra ngày 2026-11-19, mỗi document tối đa 1 MiB và API request tối đa 10 MiB; chunk hiện tại 196.608 byte giữ từng document dưới giới hạn. Việc kiểm tra tài liệu không xác nhận project credentials hay request Production cụ thể. [Firestore quotas and limits](https://docs.cloud.google.com/firestore/quotas)
3. **Job `RUNNING` hết lease:** đọc status/history nay sửa lease hết hạn thành `QUEUED` kèm `recovery_available`, hoặc đặt trạng thái terminal khi hết deadline/số lần thử. GET không dispatch worker. Nếu còn kết quả nguồn đã checkpoint, job kết thúc `PARTIAL` để giữ phần đã lưu. Checkpoint giữ theo hạn retention riêng thay vì bị cắt theo deadline thực thi job.
4. **Reload và tiếp tục có kiểm soát:** dashboard render capabilities gần nhất từ cache trình duyệt, ghi rõ trạng thái cũ và cho thử lại khi request lỗi. Cache không chứa Fetch ID, kết quả, credential hay dữ liệu riêng. Reload chỉ GET. Run History có nút **Tiếp tục Fetch** cho job đã phục hồi; thao tác POST chỉ xảy ra khi người dùng bấm, gửi lại scope nguồn trong lịch sử để server nhập lại job queued và tái sử dụng checkpoint.
5. **Phạm vi lịch sử khôi phục:** shared history nay giữ `request_kind` và `history_days`, ngoài danh sách nguồn, để UI tiếp tục đúng loại job và cửa sổ lịch sử.

## Kiểm tra

- **150 backend tests PASS** khi chạy lại trên Python của `Backend/.venv`: 119 test qua `test_d5_source_checkpoints`, `test_d5_public_admission`, `test_d5_distributed_worker`, `test_d5_firestore_shared_cache`, `test_google_trends`, `test_bing_handler`, `test_vercel_shared_live_fetch`; thêm 31 test qua `test_d5_guarded_attempt`, `test_d5_live_gateway`, `test_d5_firestore_atomic_state`.
- **24 frontend tests PASS** qua `fetch-loading-state`, `fetch-source-selection` và `run-history-coverage`.
- **6 browser modules** được kiểm tra cú pháp dưới ES modules.
- `Backend/scripts/prepare_vercel.py` chạy thành công. Bản chạy lại không in số lượng assets.
- Vercel Preview `https://trend-scout-np8wty5kt-rakoon1.vercel.app` ở trạng thái `READY`, `target: null`; build dùng cây làm việc hiện tại, không push hoặc tạo nhánh. GET tĩnh `/dashboard/` và các module `fetch.js`, `run-history.js` đều trả **HTTP 200**; HTML có `lang="vi"`, module Run History có trường và nhãn recovery.
- Playwright render Preview bằng share link tạm, với mọi `/api/**` bị chặn ngay trong browser và trả mock `503`. Shell tiếng Việt và nút thử lại hiển thị; không có request app API tới server. Ở viewport 390 px, `scrollWidth = clientWidth = 390`, không tràn ngang. 7 console errors đều là HTTP 503 do mock có chủ ý.
- URL Owner cung cấp `https://trend-scout-mu.vercel.app` ban đầu là alias **Production** ở commit cũ `b57389babc6ea308043a31c9ef66a935da3b269d` (deployment `dpl_2r294JjWURedcsGhwiaAB8zDDbmZ`). Theo chỉ dẫn của Owner dùng một link, bản D6 đã được phát hành lên alias đó.
- Production deployment `dpl_7b6cFCmW4qXCJgNuEe4Wquw6XzYV` hiện `READY`, `target: production`, aliases gồm `trend-scout-mu.vercel.app` và `trend-scout-rakoon1.vercel.app`. Vercel web fetch xác nhận HTML và module `fetch.js`, `run-history.js` trên URL Owner đưa trả **HTTP 200**; module Run History có recovery field/label. Đây là kiểm tra tĩnh, không xác nhận UI chạy cùng API thật.
- Truy vấn runtime logs mới nhất bị Vercel API từ chối `403`; tình trạng log runtime hiện **chưa xác minh**, không kết luận rằng không có lỗi.
- Playwright mở trực tiếp link Production bị chuyển tới trang đăng nhập Vercel. Preview đã được mở qua share link tạm để smoke-test UI. Không tắt Deployment Protection hay thay đổi quyền project. Production UI chưa được kiểm tra trực tiếp bằng browser phiên đã đăng nhập.
- JavaScript dashboard tự gửi GET capabilities/history khi chạy bình thường. Trong smoke test, mọi route `/api/**` bị chặn tại browser và mock 503, nên không có request app API tới server; không gửi Fetch POST, gọi provider hay Firestore thật.
- `git diff --check` sạch. Các kiểm tra backend đã chạy dùng fixture/transport giả và SQLite tạm; không gọi provider thật hay Firestore Production. Firestore Emulator suite chưa chạy trên Windows 1 vì không có Java/WSL và guard yêu cầu Linux `bwrap` với network namespace chỉ-loopback.
- GitHub Actions gần nhất trên `main` chỉ là Pages build/deployment thành công cho commit cũ `b57389b`; trạng thái đó không xác nhận code D6. Workflow Firestore Emulator đã được mở cho push `main` để có thể chạy acceptance trên commit D6.

## Gate

| Gate | Trọng số | Trạng thái | Bằng chứng |
|---|---:|---|---|
| G1 — tái hiện và khoanh vùng | 15% | PASS | Bốn failure mode được tái hiện bằng test trước khi sửa. Nguyên nhân của run Production cũ vẫn chưa xác minh được vì log hết hạn. |
| G2 — persistence/Firestore | 25% | PASS | Retry checkpoint idempotent, chunk snapshot 4,1 MB, retention độc lập với deadline. |
| G3 — lifecycle/recovery | 20% | PASS | Lease hết hạn, deadline, checkpoint đã hoàn tất, status/history không dispatch, POST tường minh để tiếp tục. |
| G4 — frontend/reload UX | 15% | PASS | Capabilities cache có nhãn stale; Run History tiếp tục theo scope lưu; reload không tự POST. |
| G5 — tích hợp/hồi quy | 15% | CHỜ | 150 backend và 24 frontend tests offline/fixture đạt; Firestore Emulator suite chưa chạy. Workflow nay có trigger cho push `main`. |
| G6 — Vercel Preview QA + Owner acceptance | 10% | CHỜ | Preview `dpl_6WFCatRwF5ZByMeMGJ76Dgb4kcPw` đã qua UI smoke với API mock; bản Production `dpl_7b6cFCmW4qXCJgNuEe4Wquw6XzYV` `READY` và alias chính trả asset recovery HTTP 200. Chưa có Owner acceptance; chưa gửi request Fetch hay kiểm tra app API/Firestore thật. |

## Việc còn lại

Preview đã được tạo bằng Vercel MCP từ cây làm việc trên `main` sau khi Vercel CLI login gặp timeout tới `api.vercel.com`. Theo chỉ dẫn Owner dùng một link, source Preview được redeploy với target Production và gán vào `https://trend-scout-mu.vercel.app/dashboard/`; deployment đó chưa có Git commit. Bước code kế tiếp là commit/push trực tiếp `main`, sau đó đợi Vercel Git deployment và Firestore Emulator workflow hoàn tất.

Owner acceptance còn lại: mở link chính trong phiên đăng nhập sẵn, reload một lần, xác nhận dashboard tiếng Việt không bị kẹt ở “Đang kiểm tra” và Run History hiển thị trạng thái đã lưu. Không nhấn Fetch. Trả lời `PASS` hoặc gửi ảnh/lỗi nếu có. Chỉ tăng tiến độ lên 100% khi acceptance và toàn bộ QA G6 đạt.

## Production deployment and rollback

- Production deployment `dpl_7b6cFCmW4qXCJgNuEe4Wquw6XzYV` is current on the single Owner-provided alias. No Fetch POST or provider request was sent during deployment verification.
- Rollback target is the prior Production deployment `dpl_2r294JjWURedcsGhwiaAB8zDDbmZ` at commit `b57389babc6ea308043a31c9ef66a935da3b269d`. If a release issue appears, use Vercel `request_rollback` for project `prj_dB1JIG2hJG8NUukGPAaqvyI1Pk3R` with that deployment ID, then verify `trend-scout-mu.vercel.app` points back to it. No rollback request was sent.
