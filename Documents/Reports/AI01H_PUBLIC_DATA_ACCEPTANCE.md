# AI01H — Public Data AI Integration

**Reviewed:** 2026-11-27 · **Branch:** `integration/demo-final` · **Baseline:** `4f405bb`

**Overall:** [83%] BLOCKED — G1, G2, G3, G5, and G6 PASS; G4 is BLOCKED by provider HTTP 429 responses. This does not authorize production activation. `TRENDSCOUT_AI_ENABLED` remains false in the example configuration, and both source approval lists remain empty.

## Acceptance gates

- **G1 — PASS.** `PUBLIC_DATA` is the server-side runtime default and cannot be selected in request bodies. The shared PostgreSQL LIVE composition created the profile on Local and Vercel; the test verifies the Vercel adapter receives `PUBLIC_DATA`. `PUBLIC_DEMO` remains isolated to attested synthetic schemas. No production ENV was changed.
- **G2 — PASS.** The dated [source-policy matrix](../Integration/AI_SOURCE_POLICY_MATRIX.md) separates fetch, retention, derived metrics, AI transfer, provider retention/training, and attribution. The registry permits only `wikimedia_pageviews_top_per_country` for live `PUBLIC_DATA`; Google Trends/Search, Bing Search, YouTube, and Wikimedia entity text are denied. Inputs use field allowlists; private request constraints and unclassified Collection context are omitted/rejected. STRICT_PRIVATE's ZDR route does not override a source's live-transfer denial.
- **G3 — PASS.** Runtime verification confirmed `particle-ai/particle-1:free` on `Particle AI` / `particle-ai/us`, active, zero-priced, structured-output capable, and within context/output limits. Requests pin that provider, set `allow_fallbacks=false`, `require_parameters=true`, `max_price.prompt=0`, `max_price.completion=0`, and use `data_collection="allow"` without request-level ZDR. The shared PostgreSQL limits remain 40 requests/UTC day and 20/rolling minute. A provider 429 now sets a shared cooldown across classification and recommendation; the runtime honors a normalized reset time or conservatively waits 60 seconds when no usable reset is returned.
- **G4 — BLOCKED.** A single official Wikimedia Analytics API snapshot for 2026-11-25 returned HTTP 200. The normalized LIVE evidence was `Miss Universe 2026`, `en.wikipedia`, `views_ceil=96,100`, top-list rank 12, scoped to US Wikimedia pageviews. Its official source URL was [the article page](https://en.wikipedia.org/wiki/Miss_Universe_2026); no raw provider payload was written. The ephemeral PostgreSQL schema contained only this keyword/evidence and a public-classified test Collection, then was dropped.

  Classification and recommendation each received HTTP 429, so neither produced a validated result or a generation ID. Token usage, model latency, resolved generation model, and actual cost are unavailable; do not interpret the zero-price guards as a successful `$0.00` generation. OpenRouter key metadata immediately before inference reported a free-tier account and usage of 0. The two physical inference POSTs consumed **2 of 4** authorized attempts. No further attempts were made after the 429s. The acceptance harness did not preserve the first response's exact retry timestamp before issuing the separate recommendation request, so backoff compliance for that second request cannot be demonstrated; the runtime-wide cooldown fix and regression tests now prevent the same cross-task retry pattern.
- **G5 — PASS.** `test_ai_engine.py` and `test_ai_offline.py`: 70 passed, 18 subtests passed. `test_postgres_ai_engine.py`: 5 passed, no skips. The focused shared Local/Vercel PostgreSQL composition profile test passed; the full composition module also passed 5 tests. Coverage includes profile and cache isolation, source and field guards, expired evidence, private-input rejection, STRICT_PRIVATE ZDR, PUBLIC_DEMO schema attestation, shared quota, and cross-task provider cooldown. The ephemeral schemas were dropped; schema `trendscout` and its quota were not modified.
- **G6 — PASS.** Ruff on changed Python files, `compileall`, and `git diff --check` passed. This report and the implementation were committed and pushed separately for AI01H to `integration/demo-final`; the remote SHA was verified at delivery.

## Source and provider decision

**Eligible live source:**

- `wikimedia_pageviews_top_per_country` — normalized top-per-country Pageviews title and bounded metric/evidence metadata under the Analytics API's CC0 grant. A US scope describes US-scoped Wikimedia pageviews only; it is not general US demand, search volume, audience residence, purchase intent, or market size.

**Restricted live sources:**

- `google_web_serp` — Google Trends and Google Search/SerpApi paths remain denied because transfer rights for those retrieved records were not established.
- `bing_web_serp` — denied; the current SerpApi path has no verified AI-transfer grant. Microsoft's retired Bing Search API terms are treated conservatively, not as proof that they govern SerpApi.
- `youtube_video_search` — remains denied under existing YouTube policy and aggregation restrictions.
- `wikimedia_entity_context` — denied until the license for each content item is verified; the Pageviews CC0 grant does not automatically cover article text.

Source/provider references were reviewed on **2026-11-27**: [Wikimedia Analytics API access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html), [MediaWiki REST API policies](https://www.mediawiki.org/wiki/API:REST_API/Policies), [Google Trends FAQ](https://support.google.com/trends/answer/4365533?hl=en), [Google Terms](https://policies.google.com/terms?hl=en-US), [SerpApi legal terms](https://serpapi.com/legal), [Bing Search API legal terms](https://www.microsoft.com/en-us/bing/apis/legal), [YouTube API Developer Policies](https://developers.google.com/youtube/terms/developer-policies), [OpenRouter provider directory](https://openrouter.ai/providers), [OpenRouter privacy](https://openrouter.ai/privacy), and [Particle AI Terms](https://particle.ai/terms) / [Privacy](https://particle.ai/privacy). Provider and source policies can change and must be rechecked before activation.

## Quota, cost, and limitations

The prior AI01G report records 14 cumulative reservations at its 2026-10-27 acceptance, outside the 2026-11-27 UTC-day and rolling-minute windows. AI01H made no quota reset and wrote only to a fresh isolated schema. That schema recorded two attempts before cleanup. Production quota state was not read or modified.

The live source snapshot is one censored daily top list and does not establish a trend by itself. No Fetch All, Google, Bing, YouTube, or private Collection context was used. There is no successful real-data inference result to evaluate. G4 must be rerun only after the recorded provider cooldown/reset has elapsed, with a new bounded request budget and fresh policy/provider checks; AI03–AI05 use mocks only.
