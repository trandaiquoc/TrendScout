# AGENTS.md

Repository-level instructions for AI agents working on **TrendScout**. These rules apply throughout this repository unless a more specific `AGENTS.md` applies to the target directory.

## 1. Instruction priority and preparation

Before changing files:

1. Follow the user's current request and applicable higher-priority instructions.
2. Read this file and check the target directory for a more specific `AGENTS.md`.
3. Read the relevant project requirements, design, decisions and existing code, if available.
4. Identify the scope, affected contracts and appropriate validation.

The user's explicit request takes precedence over repository guidance. If a consequential ambiguity cannot be resolved from the request, project evidence or reasonable judgment, explain the uncertainty and ask a focused question while completing independent work.

Do not proceed into another roadmap phase or implement product features beyond the requested task without an explicit instruction.

## 2. Product and repository scope

TrendScout aims to help users identify and evaluate emerging keywords and trends, initially for the US market, using external signals and explainable analysis. Product research and product recommendations may be addressed in later phases; their exact scope requires separate confirmation.

The initial demo is one phase of the product, not the complete product definition. A demo may use one source, manual fetching and a small dashboard. Do not present its source, formula, technical stack or hosting as an approved permanent decision unless explicitly confirmed.

Repository areas currently present:

- `Backend/` — reserved for backend and analysis work; currently no implementation has been verified.
- `Frontend/` — reserved for a future interface; currently no implementation has been verified.
- `Documents/` — product analysis, demo design and references to the legacy project.

Do not describe the current repository as a working application or claim it uses a particular framework before checking the implementation. Do not initialize the frontend, backend, infrastructure or dependencies for a documentation-only request.

## 3. Sources of truth and evidence

Use the current user request, applicable instructions, current project documentation, verified implementation and authoritative provider documentation to establish intended and actual behavior. Distinguish explicitly between:

- confirmed requirements and open questions;
- the behavior implemented in this repository, the legacy reference project and proposed future behavior;
- a source's measured population or geography and the market TrendScout intends to study;
- provider-reported values and TrendScout's own derived indicators.

The existing analysis documents are working references, not proof that a proposal was approved or implemented. Prefer the newest explicitly approved product requirements when documents disagree. Report meaningful discrepancies rather than silently inventing a resolution.

When a current external API, quota, policy, provider feature or library behavior matters, verify the relevant official documentation and state the access date and remaining uncertainty. Successful documentation lookup does not prove that the API is reachable with the project's credentials or network.

## 4. Work method and change discipline

For a feature or bug fix, make the smallest coherent change that satisfies the requested behavior. For an explicitly requested architectural refactor, audit and update the complete affected ownership boundary, its consumers, tests and contracts.

Before changing unfamiliar code, inspect the surrounding flow, data ownership, dependencies and consumers. Reuse existing project utilities where appropriate. Do not introduce an abstraction, dependency, framework or infrastructure service unless it serves the requested work.

Do not rename or reorganize unrelated files, remove user-authored documentation, commit generated build caches, or silently change an existing API, data schema, metric definition or storage contract. Keep speculative product ideas labeled as proposals.

Use clear names, focused functions, predictable error handling and existing formatting conventions. Document non-obvious decisions and external constraints. Do not suppress errors or replace missing data with fabricated values.

## 5. Data ingestion and normalization

- Put source-specific retrieval, authentication, pagination, rate limits and mapping behind an identifiable source adapter or equivalent boundary.
- Record source, collection time, applicable observation time, query or sampling scope, and source-specific geography where available. Do not infer that English content or `region=US` proves US authorship, readership or demand.
- Validate external responses, timestamps, missing fields, encodings and URLs. Treat external content as untrusted input.
- Distinguish zero, unavailable, missing, partial, stale and rejected data. Preserve error context and avoid silently publishing an older successful result as a new successful fetch.
- Deduplicate by documented rules without discarding relationships between a source record and multiple queries or topics. Explain sample limits and provider-side aggregation when they affect interpretation.
- Respect documented access restrictions, provider terms, retention requirements and deletion obligations. Do not use undocumented endpoints or circumvent access restrictions as a substitute for a working integration.

## 6. Keywords and trend analysis

- Keep normalization, candidate extraction, feature calculation and scoring distinguishable and testable. Extraction or high volume alone does not establish an emerging trend.
- Record the metric definition, observation window, baseline, source scope, formula or model version, and evidence needed to explain a result.
- Do not present a heuristic score as a probability, guaranteed accuracy, purchase intent or universal US trend unless independently validated for that claim.
- Define support and minimum-data rules; avoid dividing by zero and producing confident labels from one observation. Missing baseline or incompatible source/time windows must remain visible.
- When combining sources, do not mix incompatible metrics or claim US coverage without evidence. Keep source-level evidence and document normalization and weighting.
- Keep product recommendations separate from measured trend signals and label future or unverified capabilities accordingly.

## 7. Security and configuration

Never commit passwords, API keys, access tokens, database credentials, private keys or production secrets. Do not expose them in frontend code, logs, fixture files or generated documentation.

Use environment variables or the hosting platform's secret mechanism for credentials when an integration requires them. Restrict user-triggered fetches, validate input and outbound destinations, and avoid exposing unrestricted credentialed proxies or unbounded API requests.

## 8. Dependencies, hosting and operations

For a new dependency, confirm that it is necessary, maintained and compatible with the actual runtime; explain material trade-offs. Record dependency versions with the project’s chosen lock mechanism.

Treat free hosting, quotas and pricing as changeable. Verify cold starts, ephemeral filesystem behavior, service limits, persistence and provider permissions before relying on them. A free host does not grant access to a third-party data API.

Do not deploy, enable paid services or make external changes unless the user has authorized the relevant action. Prepare reviewable local work first when approval is required.

## 9. Testing and verification

After meaningful changes, run the relevant tests, build, type checks or document validation available for the affected scope. Report failures and limits; do not claim a live integration or UI works when it has not been tested.

For trend features, prioritize meaningful cases: duplicate records, inconsistent timestamps, 0 versus missing values, insufficient baseline, volume changes, noisy spikes, rate limits, retry failures, stale cache and deterministic scoring. Use synthetic or permitted fixtures for offline verification; label replay data clearly and test live provider access separately.

Permanent tests should protect observable behavior and independent contracts. Temporary probes and generated build artifacts should not become canonical source files.

## 10. Documentation and handoff

Update relevant documentation when changing user-visible behavior, a public API, schema, scoring formula, setup, credentials, source coverage or retention. Mark draft, proposed, implemented and verified states clearly.

For analysis documents, state assumptions, requirements and acceptance criteria, alternatives, data limitations, phase boundaries, risks and open decisions. Cite authoritative sources close to externally sourced claims. Do not assert that a target architecture, cost or delivery date has been approved when it has not.

When finishing an implementation task, report what changed, how it was checked and material limitations. Continue only within the user's requested scope.
