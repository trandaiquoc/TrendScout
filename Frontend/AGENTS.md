# Frontend AGENTS.md

These instructions apply to work under `Frontend/` and extend the repository-level `AGENTS.md`. Follow the user's current request and read the relevant product requirements, API contracts and existing frontend implementation before changing code.

## 1. Scope and stack

`Frontend/` is reserved for the TrendScout user interface. At the time this file was adapted, no frontend implementation or framework was verified in this directory. Do not describe Angular, React, Vue, TypeScript or another framework as already installed or selected.

When implementation exists, follow its actual framework, version, style and testing conventions. Select or introduce a framework, design system, state-management library or charting dependency only when the user's task requires it or the project has explicitly adopted it. A proposed demo stack is not a permanent product decision.

The interface may evolve from a small manually refreshed demo into a broader trend-analysis dashboard. Implement only the requested phase and preserve a clear boundary between presentation and analysis.

## 2. Product behavior and data interpretation

- Support the active scope defined by the user and product documentation. Do not add product recommendations, multi-source aggregation, alerts or account features merely because they appear in future plans.
- Show the provider, observed or collected time, data scope and limitations when they affect interpretation. Distinguish US-targeted data from data proven to describe US users or audiences.
- Present a trend score as an application-defined indicator with its evidence, comparison window and level of data sufficiency. Do not display it as a probability, guaranteed accuracy or purchase intent without supporting validation.
- Distinguish loading, empty, insufficient data, partial, stale, failed and successful states. Do not show cached or replayed data as a fresh live result.
- Keep current and previous results identifiable during a refresh. Do not hide a failed refresh or substitute zero for unavailable values.
- Do not duplicate scoring rules in the UI. Display the backend's versioned result and explanation; any presentation-only transformation must not change the underlying meaning.

## 3. Components, data access and state

- Keep components focused on a coherent responsibility and prefer semantic, intention-revealing names.
- Keep HTTP/data access and external-response validation separate from presentation when the adopted architecture supports that boundary.
- Model untrusted external data separately from validated internal state. If TypeScript is used, avoid `any`, narrow `unknown` and avoid unsafe assertions that only silence errors.
- Keep state transitions predictable; handle cancellation, duplicate submissions and component cleanup according to the chosen framework.
- Reuse existing components and design tokens. Avoid speculative abstractions and generic helper directories for unrelated concepts.
- Make ordinary local search responsive and avoid an upstream API request per keystroke. Make a remote analysis or manual refresh an explicit action.

## 4. Accessibility and interaction

Target WCAG 2.2 AA for implemented screens when feasible. Use semantic HTML, meaningful labels, visible focus, keyboard-operable controls, accessible error and status messages, and adequate contrast. Do not communicate important status by color alone.

For charts, provide an accessible text or table alternative with units, observation window and missing-data indication. Use correct button/link semantics and handle focus for dialogs. Test accessibility with the tooling available in the repository; report any unverified areas.

## 5. Presentation and localization

- Follow the established layout, responsive behavior and design-token system when one exists. Do not invent multiple themes or persisted appearance preferences.
- Keep numeric units, timestamps, time zones and source labels understandable. Format values consistently without altering the source data or masking important uncertainty.
- Keep user-facing copy separate from business logic where the adopted localization approach supports it. Vietnamese is relevant to current project communication; additional supported languages require a product decision.
- Provide meaningful alternative text for informative images and empty alt text for decorative images.

## 6. Security and external content

- Never embed provider API keys, database service credentials, access tokens or other secrets in frontend code, logs or fixtures.
- Treat API responses, URLs, user-provided keywords and imported data as untrusted. Avoid unsafe HTML injection and validate or safely render external links and content.
- Call approved application endpoints; do not bypass backend authorization, rate limits or provider access rules. Do not build an unrestricted URL proxy.
- Do not expose private user data, raw responses or provider content beyond the product's approved scope and retention policy.

## 7. Performance and verification

Optimize measured or clearly expected bottlenecks while preserving correctness. Avoid expensive work on every render, needless network calls and unmanaged event handlers or subscriptions. Use pagination or virtualization only when the actual data volume justifies it.

After meaningful frontend changes, run the available build, type checks and relevant tests. Verify critical interactions, responsive layout and accessibility when practical. Useful scenarios include refresh success/failure, duplicate clicks, local search, incomplete data, stale cache, replay labels and chart/table consistency.

Report what was verified and any limitations. Do not claim live API, browser compatibility or accessibility conformance without the corresponding evidence.

## 8. Architecture changes

For an ordinary feature or fix, stay within its responsibility boundary and avoid unrelated framework, folder or infrastructure changes. For an explicitly requested architecture refactor, inspect the complete affected scope, update consumers and contracts, and validate that ownership and behavior have actually moved to the intended boundary.

Keep permanent regression tests that protect real behavior. Temporary probes, debug files and generated artifacts belong outside canonical source and documentation, and should be cleaned up after use.
