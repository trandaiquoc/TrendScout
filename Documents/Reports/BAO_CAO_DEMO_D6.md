# D6 — Personal live demo: báo cáo thực hiện

Ngày cập nhật: 12/11/2026. D6 là tích hợp live local cá nhân; D7 là nghiệm thu end-to-end demo live. Company production và nghiệm thu multi-host thực là track riêng. D5 synthetic demo vẫn độc lập với D6.

## Trạng thái

| Gate | Trạng thái | Bằng chứng và giới hạn |
| --- | --- | --- |
| OFFLINE regression (historical) | PASS, not rerun in D6.1B | Bản ghi trước đó gồm 634 unittest, Golden hai hash seed và browser E2E; không phải bằng chứng cho checkout/toolchain hiện tại hoặc provider live. |
| Cấu hình credentials | PRESENT | Hai key hiện diện trong `.env` local được Git bỏ qua và có quyền 600; hiện diện không chứng minh key hợp lệ hoặc quyền/quota. Không ghi giá trị. |
| D6.1B local HTTP implementation | IMPLEMENTED, verification pending | `create_local_live_app` nối facade D6.1 qua `LocalLiveGuard`; kiểm thử HTTP offline mới được thêm nhưng môi trường hiện tại thiếu dependency và không có Python 3.12. Không gọi provider. |
| LIVE_CONFIGURED | BLOCKED | Local HTTP wiring đã có; chưa xác minh provider thật, chính sách sử dụng, credentials/quota thực tế hoặc giao diện dashboard live. |
| LIVE_SMOKE_VERIFIED | BLOCKED | Chủ dự án đã phê duyệt tối đa bốn request, không retry/phân trang, ngân sách phát sinh 0 đồng và xác nhận quota SerpApi; runtime chưa sẵn sàng và yêu cầu sau đó cấm gọi YouTube live hoặc dùng key thật. Chưa gửi request tới provider. |
| LIVE_DEMO_ACCEPTED | BLOCKED | Chưa có bằng chứng live bốn nguồn. |
| Company production multi-host | BLOCKED, track riêng | Không phải điều kiện chạy demo local. |

Các adapter và gateway D5 đã có, song Public Fetch, response provenance và discovery hiện chủ ý khóa synthetic. D6 cần tích hợp local có kiểm soát và xác minh chính sách trước khi bật live; không thay đổi Vercel read-only. Discovery hỗ trợ đầu vào live có grant backend theo nguồn và giữ nguồn gốc mixed; không tự cấp grant. Thiếu quyền trích xuất thì loại record khỏi discovery; thiếu quyền metric thì giữ `INSUFFICIENT_EVIDENCE`.

`LocalLiveGuard` hiện bọc riêng `create_local_live_app`; từ chối peer không loopback, Host không tin cậy, proxy headers, cross-origin, HTTPS, route và method ngoài allowlist. Đây không phải xác thực và không chặn local direct clients. Factory yêu cầu opt-in, local profile, facade LIVE hợp lệ và một worker; ứng dụng `create_app()` mặc định và Vercel không nhận được đường live này. HTTP integration hiện có test fixtures, chưa có provider live evidence.

Đã chuẩn bị policy smoke mặc định tắt với một lượt Wikimedia, hai lượt chung SerpApi, một lượt YouTube theo ngày/tháng, một job mỗi 24 giờ và mỗi pool tối đa một attempt đồng thời. `LocalSmokeStore` giới hạn một reservation cho từng nguồn trong ledger, giữ bộ đếm qua khởi tạo lại và không cho Google dùng lượt của Bing; test dùng SQLite local. Cost units nội bộ chỉ đếm attempt, không phải tiền hoặc quota còn lại của provider. Policy và wrapper chưa được gắn vào runtime; ledger thật còn cần được bố trí tại đường dẫn owner-only bền vững trước smoke.

SQLite có factory riêng cho dữ liệu live local, yêu cầu thư mục owner-only và từ chối Vercel; factory không bật API. Cần thiết kế và kiểm chứng xóa dữ liệu, kể cả WAL sau crash, trước khi lưu dữ liệu provider thật. Ledger quota cần tồn tại qua restart; không coi xóa ledger là cách cấp thêm ngân sách.

`LocalVolatileCheckpoints` giữ checkpoint nguồn ở bộ nhớ tiến trình, còn quota receipt lưu trong SQLite; lock ngăn hai tiến trình cùng dùng lớp này và dữ liệu checkpoint mất khi restart. Facade live yêu cầu lớp này thay vì nhận SQLite trực tiếp, và gateway chấp nhận lớp bọc tương ứng. Kiểm thử offline kiểm tra facade từ chối SQLite trực tiếp và dữ liệu fixture không được ghi vào file ledger. HTTP factory dùng lại facade này; kiểm thử không chứng minh xử lý crash, swap, core dump hoặc retention của dữ liệu provider thật.

Kiểm thử ghép offline `test_d6_offline_worker_composition` chạy worker với bốn gateway `MockTransport`, quota smoke, checkpoint tạm và query plan cố định: mỗi nguồn có một request fixture, duplicate cùng job, và nội dung fixture kiểm tra không xuất hiện trong ledger. Các kết quả được gắn `SYNTHETIC`; đây không phải bằng chứng live hoặc bằng chứng chính sách provider đã được chấp thuận.

Gateway phân biệt scope `LOCAL_SMOKE` với `PRODUCTION`: local bắt buộc policy smoke cố định, wrapper giới hạn nguồn và SQLite live local, cùng các grant server-side khác; production tiếp tục đòi bằng chứng distributed controls. Tạo scope này không tự cấp phê duyệt provider và không chứng minh an toàn production.

`scripts.d6_local_smoke` là công cụ smoke đề xuất, mặc định từ chối chạy; test mock kiểm tra bốn lần gọi tối đa và không lưu body response vào ledger. Chỉ dùng sau khi chủ dự án phê duyệt bốn request, xác nhận quota, không phát sinh phí và quyền sử dụng; không suy ra approval từ việc có key. Công cụ chưa được chạy live và chưa thay thế kiểm tra pagination hoặc D7 E2E. Mỗi ledger chỉ chấp nhận một attempt mỗi nguồn; không xóa ledger để vượt giới hạn.

Dashboard đã chuẩn bị để nhận `LIVE`/`MIXED` chỉ khi capability xác định rõ demo local và không read-only; nguồn đang hoạt động được ghi `Live local`, dữ liệu thiếu mode không bị tự gắn `SYNTHETIC`. Kiểm thử gate UI là offline, chưa chứng minh live UI hoặc YouTube branding compliance.

Fetch facade hỗ trợ contract live local chỉ khi dùng policy smoke và ledger local tương ứng; live result chỉ trả tóm tắt từng nguồn, không trả records thô. Discovery vẫn cần processing grant theo nguồn. Đây mới là kiểm thử contract với dữ liệu giả lập, chưa nối vào API/dashboard hay gọi nguồn thật.

API capability và response tiếp nhận Fetch lấy data mode từ facade được cấu hình, thay vì luôn gắn `SYNTHETIC`. Factory thường tiếp tục chặn live; factory local riêng chỉ mở đường LIVE sau opt-in và composition context rõ ràng. Khi checkpoint không còn, status không suy ra `LIVE` chỉ từ retention grant. Bộ test HTTP fixture mới bao phủ capabilities, Fetch/replay, status, redaction, guard, startup không chạy handler và cleanup; chưa thực thi được do thiếu `httpx` và Python 3.12 trong môi trường hiện tại.

Khi gửi lại idempotency key, admission trả trạng thái job thực tế trong cùng transaction; API không khởi động lại background worker cho job đã kết thúc. Kiểm thử API synthetic xác nhận replay sau khi hoàn tất báo cùng trạng thái với endpoint status. Điều này không thay thế kiểm thử live hoặc supervisor sau crash.

`local_live_composition` ghép facade, worker, bốn gateway, query plan cố định, quota và checkpoint tạm thành một context backend; constructor yêu cầu các approval/grant do backend cấp, không tự đọc key hoặc gửi request. Context đóng gateway và giải phóng lock khi thoát; Vercel bị từ chối. Kiểm thử offline xác minh thiếu approval bị chặn và không có request trong lúc khởi tạo. Smoke CLI và composition dùng cùng đường dẫn và namespace ledger cố định; kiểm thử cho thấy hai phía cùng thấy job và reservation. Đây chỉ là ranh giới trong mã local: người có quyền máy có thể sửa code hoặc xóa ledger, nên cần kiểm soát vận hành; không coi đó là chống lạm dụng production. Chưa có HTTP runtime, policy consent hay kiểm chứng provider; không dùng context này để tuyên bố live ready.

Composition giới hạn thời hạn logic của checkpoint grant tối đa 120 giây và giữ nguyên grant ngắn hơn hoặc thời điểm hết hạn sớm hơn; grant bị từ chối, sai nguồn hoặc sai mode không được biến thành quyền hợp lệ. Bộ quét chạy định kỳ, do đó đây không phải chứng minh xóa vật lý chính xác sau 120 giây hoặc xóa mọi bản sao trong bộ nhớ, swap, log và crash dump. Chính sách công khai vẫn chờ chủ dự án duyệt và kiểm chứng toàn bộ đường đi dữ liệu.

Phân loại `topic-terms-v2` bổ sung từ khóa mẫu cho đủ 13 nhóm chủ đề đang hoạt động; đây là heuristic chưa được đánh giá độ chính xác, có thể gán nhiều nhóm hoặc `unclassified`, không chứng minh hiểu hết mọi chủ đề. Định danh keyword và phiên bản thuật toán trích xuất không đổi; Fashion và Home & Living vẫn được ưu tiên nghiên cứu theo quy tắc hiện có.

Bộ trích xuất vẫn dùng heuristic hướng tiếng Anh; bản ghi LIVE đi vào discovery hiện được gắn ngôn ngữ `und` (chưa xác minh) và keyword có cờ `LANGUAGE_UNVERIFIED`. Bộ lọc `hl=en`, `regionCode=US` hoặc vị trí của Wikimedia không xác minh ngôn ngữ từng nội dung hay nơi cư trú của tác giả/người xem. Nhãn này không phát hiện ngôn ngữ và không chứng minh độ chính xác của heuristic trên nội dung đa ngôn ngữ; không gọi kết quả là nhu cầu của toàn bộ thị trường Mỹ.

## Cấu hình local

Chủ dự án tự chỉnh `.env` tại thư mục gốc; file được `.gitignore` loại trừ. `LocalCredentialProvider` chỉ đọc hai tên `YOUTUBE_API_KEY` và `SERPAPI_API_KEY`, không thực thi shell, từ chối symlink, file không thuộc chủ hiện tại, quyền cho group/other hoặc định dạng không hợp lệ. Chạy từ `Backend`: `./.venv/bin/python -m scripts.check_demo_credentials`. Kết quả chỉ là `PRESENT`, `MISSING` hoặc `INVALID_CONFIG`.

## Provider, chi phí và chính sách

Tài liệu chính thức đối chiếu 12/11/2026; mức quota thực tế và điều khoản tài khoản cần chủ tài khoản xác nhận:

- [Wikimedia Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html) yêu cầu User-Agent có thông tin liên hệ và khuyến nghị gửi tuần tự; chưa kiểm chứng khả năng liên hệ của URL cấu hình.
- [SerpApi pricing](https://serpapi.com/pricing) mô tả gói Free 250 searches/tháng và Starter $25/tháng cho 1.000 searches. Search thành công tính quota; kết quả cached/error/failed không tính theo trang này. Gói, credit còn lại, overage và giá thực tế của tài khoản chưa được xác nhận. Google/Bing dùng chung pool.
- Điều khoản sử dụng và quyền xử lý dữ liệu SerpApi cho trường hợp demo này chưa được xác nhận; trang điều khoản không truy cập được trong lần kiểm tra, nên không xem quyền đó là đã được cấp.
- [Google](https://serpapi.com/search-api) và [Bing](https://serpapi.com/bing-search-api) SerpApi có thể phục vụ cache dù trạng thái là `Success`; gateway live đặt `no_cache=true` để yêu cầu truy vấn mới, dự trù mỗi request có thể trừ credit. Cache synthetic được ghi rõ; không suy ra thời gian tìm kiếm thực tế chỉ từ lúc backend nhận response.
- [YouTube quota](https://developers.google.com/youtube/data/determine_quota_cost) hiện mô tả `search.list` dùng bucket riêng mặc định 100 calls/ngày, mỗi call 1 đơn vị; trang quota tài khoản mới là nguồn cho quota thực tế. Không tự bật billing.
- [YouTube Developer Policies III.E.2 và III.E.4.h](https://developers.google.com/youtube/terms/developer-policies) giới hạn tổng hợp và tạo dữ liệu/metrics dẫn xuất từ API Data. Áp dụng chính xác với keyword extraction cần xác nhận quyền/chính sách; không tự suy diễn rằng API key cấp quyền đó. [Chính sách lưu trữ](https://developers.google.com/youtube/terms/developer-policies) cũng quy định thời hạn tối đa cho nhiều loại dữ liệu không được ủy quyền. Không cấp grant xử lý live khi chưa xác minh; hiển thị dữ liệu YouTube riêng còn cần kiểm tra yêu cầu branding và chính sách.

Chủ dự án đã phê duyệt smoke tối đa **một request mỗi nguồn**, bốn request tổng, không retry/pagination; tối đa hai search SerpApi và một YouTube `search.list`, ngân sách phát sinh 0 đồng, và xác nhận quota SerpApi không tính phí vượt gói. Phê duyệt này không xác nhận quyền xử lý YouTube, không mở rộng sang pagination và không vượt qua yêu cầu sau đó của chủ dự án cấm gọi YouTube live hoặc dùng API key thật. Smoke chưa chạy; cần runtime và quyền provider phù hợp trước khi thực hiện các phần được phép.

## D6.1B local HTTP runtime — implementation and acceptance (12/11/2026)

`create_local_live_app(enabled=True, composition_factory=...)` is a separate opt-in path in `app.main`. It requires local settings, live run mode, no cloud project/read-only/public-fetch flag, one worker, and a context factory that yields the existing `DistributedPublicFetch` local-live facade. It does not default credentials or permissions, and app construction/startup does not execute handlers. `run_local_live()` binds `127.0.0.1`, one Uvicorn process, and disables proxy-header trust. The ASGI guard permits only capabilities, Fetch submission, Fetch status, and health routes over loopback HTTP with local Host and no forwarded or cross-origin context.

| Gate | Current result | Evidence / limitation |
| --- | --- | --- |
| G1 Git baseline | PASS | `origin/main` fetched; clean local tree at `2485281497813e1d82106ebc0b716c22d72572b1`; no divergence. |
| G2 HTTP integration | IMPLEMENTED, pending execution | Focused fixture test covers capabilities, POST/status/replay, startup with zero handler calls, source isolation, redaction, and context cleanup. A new end-to-end test composes the actual local D6 gateways and worker while replacing `AsyncHTTPTransport.handle_async_request` with fixture responses. Python test import is blocked by missing `httpx`. |
| G3 Security | IMPLEMENTED, pending execution | Factory/ASGI checks cover opt-in, local/live/non-read-only profile, Vercel, one-worker configuration, loopback peer, Host, proxy/origin/scheme and allowed methods/routes. HTTP cases are written but not run. |
| G4 Quota and idempotency | PARTIAL | Existing D6 code keeps admission/quota receipts in the SQLite ledger and volatile checkpoints in process memory. New HTTP replay case and relevant regressions could not run here. |
| G5 Provenance and data | PARTIAL | Existing facade keeps LIVE/source status and removes raw records from LIVE HTTP results; processing grants are still caller-supplied. New HTTP redaction and isolated-source cases could not run here. |
| G6 Regression | BLOCKED BY LOCAL TOOLCHAIN | Host has Python 3.14.5 and no installed project packages; repository requires Python 3.12. The focused test failed at `import httpx`. `Backend/.venv` is absent; WSL cannot start (`REGDB_E_CLASSNOTREG`) and Docker/Podman are unavailable, so `verify_offline_readiness.sh`, golden, browser E2E and `pip check` were not run. Python syntax compilation and `git diff --check` passed. |
| G7 Handoff | PASS | Startup/limitations and this report are documented. Implementation commit: `c5dc167dbb99700f4cb2b3817da5f9dc6dfd60e0`. Tree clean after commit; no push/deploy/provider request. |

The HTTP runtime reuses the existing fixed query plan, one-job policy, shared SerpApi attempt pool, source approvals, checkpoint grants, volatile checkpoint store, and SQLite ledger. Terminal idempotent replays do not schedule another worker; status reads do not invoke source handlers. Missing source approval remains unavailable and missing processing grants do not create derived candidates. These behaviors are specified in the implementation, but current execution evidence is pending because the required Python runtime and dependencies are unavailable.

Retention remains logical and bounded: source checkpoints are process memory and disappear on restart; SQLite persists quota/attempt receipts and must not be reset to regain request allowance. This goal does not prove physical erasure from SQLite WAL files, filesystem backups, swap, crash dumps, logs, or other copies. Loopback is not caller authentication against local processes. The runtime is personal/local only, not a public endpoint or production coordination system.

### Remaining blockers and next goal

Before treating D6.1B as accepted, run the new focused tests and the required offline regression with the repository's Python 3.12 environment. No provider request was made in this goal. D6.2 remains separately gated on source-specific operator permission, data processing/retention approval, credential/quota verification, and an explicit future instruction authorizing that source's live verification. The recommended next action is to establish the Python 3.12 offline environment and review this local commit; do not start D6.2 automatically.

## D6.1B-WIN native Windows primary environment — verification (12/11/2026)

This section supersedes the earlier pending-toolchain status above for the Windows gates only. Windows is the primary development and local execution environment; Linux remains secondary for compatibility and Linux-only security checks. Verification used native Windows PowerShell 5.1, Python 3.12.10, the project virtual environment, Node.js, and installed Chrome. Python 3.14 was left installed. No WSL, Docker, Podman, provider request, real API key, paid call, deployment, or push was used.

For this Windows execution, `git fetch origin` succeeded and the starting local `HEAD` matched `origin/main` at `7e579921395d54aa08dde4f359d7f0cfd7f8f020`; the starting tree was clean and had zero divergence. The implementation was committed locally as `f04ab7376c8be87ce68597779a09656bddfd3710`; no push was made.

| Gate | Result | Evidence / limitation |
| --- | --- | --- |
| G1 Native Windows environment | PASS | `setup.ps1` installed pinned requirements into `Backend/.venv`; `py -3.12 --version` reported 3.12.10; `pip check`, `httpx`/FastAPI imports and SQLite in-memory query passed. Setup creates no credential file. |
| G2 Runtime and startup | PASS | Native imports and runtime composition passed. `start.ps1 -Synthetic -Port 8765` started Uvicorn on loopback; `/api/health` returned 200 and `/dashboard/` returned 200. Ctrl+C completed Uvicorn shutdown. The synthetic runtime used fixtures and made no provider call. |
| G3 Locking, SQLite and Windows ACL security | PASS on Windows | The 32 focused D6 tests passed. Native Windows security descriptor checks verified current owner SID, protected DACL, allowed user/SYSTEM/Administrators/owner-rights ACEs, rejection of other principals and reparse points. Cross-process byte-range locking, crash release, credential ACL handling, SQLite atomic state, and database/WAL/SHM private ACL checks passed. Linux POSIX branches remain implemented. |
| G4 Guarded local HTTP | PASS offline | Focused fixture and composed HTTP tests passed within the D6 set. Requests were fixture-backed; the normal synthetic startup did not opt into local LIVE or activate a provider. |
| G5 Portable regression | PASS | Complete unittest suite: 642 tests passed. |
| G6 Golden, browser, JavaScript and benchmark | PASS on Windows | Golden contract suite: 7 tests passed with each `PYTHONHASHSEED=1` and `983`. `pip check` and JavaScript syntax checks passed. Five offline Chrome DOM/keyboard/API browser E2Es passed. The D2 visual capture script also passed using temporary output. Synthetic storage benchmark completed at 1k/5k/10k entities; elapsed 0.628/2.408/4.836 seconds, respectively (single local run, not a performance guarantee). |
| G7 Linux tests preserved and documented | PASS in source/documentation | Linux seccomp, resource-limit and syscall checks remain in Linux scripts and are identified as a separate secondary gate. They were not run on this Windows host. |
| G8 Linux runtime compatibility | NOT RUN | POSIX implementations and Linux test branches were retained, but no Linux host was available for execution in this task. Linux runtime compatibility is therefore pending a native Linux verification run; no seccomp PASS is claimed. |

Windows uses `msvcrt` byte-range process locks and native Windows security descriptors/ACLs rather than treating `chmod` as private-file security. POSIX uses `fcntl.flock` and ownership/mode checks. On Windows, local credentials and the D6 ledger default to `%LOCALAPPDATA%\TrendScout`; SQLite WAL/SHM and lock files are created under protected directories and checked. The browser E2Es use local fixtures and Chrome DevTools; Linux network-denial tests continue to use their existing Linux isolation mechanism. `setup.ps1`, `start.ps1`, `verify_offline_readiness.ps1`, root `README.md` and `Backend/README.md` document the Windows-first workflow and separate Linux gate.

No D6.2 work was started. The guarded local LIVE HTTP runtime remains a distinct explicit opt-in requiring the existing valid local approvals and grants; the ordinary synthetic startup does not enable it. The remaining acceptance blocker is G8 execution on Linux, which must be completed on a native Linux environment before claiming the full cross-platform goal is complete.
