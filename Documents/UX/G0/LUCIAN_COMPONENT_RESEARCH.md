# Lucian Labs component research

**Reviewed:** 2026-11-13 · **Purpose:** interaction reference for later UI work. Do not import the full theme or copy demo content/data.

## Source and license

- Repository: [lucian-uxui-prompt](https://github.com/Seokwoooo/lucian-uxui-prompt)
- Introduction: [Lucian UX/UI Prompt](https://lucianlabs.dev/en/uxui-prompt/introducing/)
- The current repository describes **400 elements** (200 interface parts and 200 website sections/animations); an older intro result says 200, so recheck the catalog count at adoption.
- Catalog: [English catalog](https://raw.githubusercontent.com/Seokwoooo/lucian-uxui-prompt/main/api/v1/en/catalog.tsv)
- License: [MIT LICENSE](https://raw.githubusercontent.com/Seokwoooo/lucian-uxui-prompt/main/LICENSE), © 2026 Seokwoooo. Preserve the license and attribution comment for copied/adapted code; verify the current license at adoption.
- The repository documents vanilla HTML/CSS/JS snippets, scoped `--ux-*` tokens/classes, keyboard/pointer/touch behavior. TrendScout is also vanilla web code, but snippets still need review for tokens, localization, accessibility and security. The source library does not require a dependency.

## Selected references

| Component | Source | Proposed use | Adaptation/caution |
| --- | --- | --- | --- |
| Empty search that loosens filters (#092) | [e/092](https://raw.githubusercontent.com/Seokwoooo/lucian-uxui-prompt/main/api/v1/en/e/092.txt) | No-results state suggests which filters to clear and preserves query | Map to TrendScout vocabulary; no irrelevant sample cards |
| Command palette (#046) | [e/046](https://raw.githubusercontent.com/Seokwoooo/lucian-uxui-prompt/main/api/v1/en/e/046.txt) | Optional later quick navigation/search | Not needed for initial IA; requires keyboard, Escape, arrows, Enter, ARIA, localization |
| Expanding topbar search (#027) | Catalog entry #027 | Progressive search affordance | Keep Collection search always visible; do not hide core task behind animation |
| Sliding tabs (#039) | Catalog entry #039 | Dashboard Overview/Analysis affordance | Semantic tabs, keyboard support and reduced motion; no decorative-only movement |
| Skeleton transition (#079) | [e/079](https://raw.githubusercontent.com/Seokwoooo/lucian-uxui-prompt/main/api/v1/en/e/079.txt) | Loading table/card geometry | Replace social-card copy with final product structure; honor reduced motion |
| Retry state (#090) | Catalog entry #090 | Error recovery presentation | Retry only when backend admission/policy allows; no repeated automatic calls |
| Bar/line chart (#095/#096) | Catalog entries #095/#096 | Interaction ideas for tooltips and data emphasis | Examples are fictional. Do not reuse numbers, deltas or trend claims; require compatible real observations and accessible table. |

## Not recommended

- Liquid-glass button (#001): decorative material conflicts with restrained data workspace and can reduce contrast.
- Hold-to-delete (#003): hidden destructive interaction; use explicit confirmation and impact summary.
- Finance/stock charts: their axes/status colors do not define TrendScout evidence semantics.
- Any snippet’s sample names, numbers, market data, imagery or trend claims.

## Adoption checklist

1. Copy only selected source; review every line; retain MIT notice/attribution.
2. Map styles to existing TrendScout CSS variables and scope selectors.
3. Replace visible text with VI/EN keys; use actual backend state.
4. Verify semantics, keyboard, WCAG 2.2 AA contrast, focus, reduced motion and mobile width.
5. Remove demo data, tracking, unsupported dependencies, external fetches and unsafe HTML insertion.
6. Recheck license/version and component source before merge.
