# AI02 — Batch Processing & Quota

Overall: [100%] AI02 PASS — G1–G6 PASS; mock results are not model-quality evidence.
AI01 remains [83%] BLOCKED from its provider inference 404; AI02 does not change that status.
- G1 PASS — batches max 40, capped by output tokens, exact prompt bytes and context; profile uses provider latency, not quota/storage deferrals.
- G2 PASS — valid classifications are reused; only changed groups reclassify; Collection/evidence/user state remains in cache validity; unrelated algorithm metrics do not invalidate.
- G3 PASS — shared PostgreSQL quota, concurrent reservations and failed-attempt accounting; 40/day and 20/rolling-minute remain enforced.
- G4 PASS — quota and interrupted-job resume preserve completed batch cache, pending IDs and idempotent persistence without rerunning Fetch.
- G5 PASS — A–G processed all eligible IDs; 1,500 cold uses 38 requests, within 40; 10% changed uses 4.
- G6 PASS — focused tests, PostgreSQL tests, Ruff, compileall, diff check and remote branch SHA verified at delivery.
Quota benchmark clock advanced 3.1s per mock request; G's simulated day rollover wait is excluded from wall latency. Peak rolling-minute use was 19/20.
Estimated input tokens = ceil(prompt UTF-8 bytes / 3.2) + 256 per request; output tokens are estimated from mock JSON bytes. SQL/transactions exclude migrations, seed and mutations.

|Case|Groups|Cache hit/miss|Requests|Prompt bytes|Input/output tokens|Processed/pending|Wall seconds|SQL/tx|
|---|---:|---:|---:|---:|---:|---:|---:|---:|
|A|100|0/100|3|173,307|54,928/4,691|100/0|7.673|219/23|
|B|500|0/500|13|864,397|273,458/23,431|500/0|34.967|959/93|
|C|1,500|0/1,500|38|2,592,122|819,783/70,281|1,500/0|115.815|2,809/268|
|D|1,500|1,500/0|0|0|0/0|1,500/0|14.167|200/40|
|E|1,500|1,485/15|1|27,184|8,751/710|1,500/0|18.773|244/46|
|F|1,500|1,350/150|4|266,026|84,159/7,031|1,500/0|23.882|467/64|
|G|2,500|80 resume hits|63|4,319,847|1,366,108/117,131|2,500/0|200.832|4,694/452|
G deferred after day-one request 40 with 980 IDs pending; resume reused 80 cached groups and sent only the remaining 120 in 3 requests. Day two used 23 requests.
Tests: AI unit 31 passed + 2 subtests; PostgreSQL AI 3 passed, 0 skipped; Ruff, compileall and git diff check passed.
Integrity: all expected keywords/evidence links remained; zero duplicate evidence links. No OpenRouter or source provider calls; all benchmark inference used the local mock.
