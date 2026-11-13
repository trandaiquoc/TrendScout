# Design System Implemented in G1

## Foundation

The current warm neutral palette remains the TrendScout light theme, with its coordinated dark palette. G1 reuses the established semantic color variables for canvas, surfaces, text, muted text, borders, accents, focus, and status colors. Both themes use the same spacing and component tokens.

| Token | Value |
| --- | --- |
| `--space-1`, `--space-2`, `--space-3`, `--space-4` | 4, 8, 12, 16 px |
| `--space-6`, `--space-8`, `--space-10` | 24, 32, 40 px |
| `--radius-sm`, `--radius-md`, `--radius-lg` | 8, 12, 18 px |
| `--type-display` | `clamp(30px, 4vw, 48px)` |
| `--type-heading`, `--type-body`, `--type-label`, `--type-caption` | 24, 14, 11, 10 px |
| `--content-max` | 1510 px |
| `--motion-fast` | 140 ms ease |

## Applied patterns

- **Workspace shell:** 254 px expanded sidebar, 76 px collapsed desktop sidebar, sticky topbar, bounded content width, and mobile drawer.
- **Dashboard tabs:** one selected pill tab, visible hover and active states, keyboard focus, and selected state that does not rely on color alone.
- **Overview metrics:** five equal-width cards on wide screens and a two-column grid at smaller breakpoints; numbers use tabular formatting and missing data remains an em dash or localized unavailable label.
- **Analysis cards:** consistent panel heading hierarchy, filter frame, loading skeleton, evidence-based value state, and explicit empty/unsupported states.
- **Source cards:** one descriptor-driven status list with separate rows for adapter, configuration, approval, local verification, current-operation enablement, current health reporting, and production readiness.
- **Controls:** existing buttons, icon buttons, selects, search fields, disclosure panels, tables, notices, dialogs, and badges continue to share established styles. Disabled filters communicate that supported analysis data is not available yet.
- **Motion:** tab and shell transitions use the shared duration token; the existing reduced-motion media rule shortens transitions and animations.

## State and accessibility treatment

Interactive controls retain visible `:focus-visible`, hover, active, and disabled treatment. The mobile drawer uses inert background navigation, keyboard focus containment, Escape closing, and focus return. Tabs expose `tablist`, `tab`, and `tabpanel` semantics, `aria-selected`, and roving tab index. Buttons provide native Enter/Space activation; arrow keys and Home/End move between tabs.

The G1 Analysis frame does not render empty chart axes or demo values. It displays a single-source snapshot count only when the actual saved snapshot includes a retained count and one source. Each unsupported analysis card explains the evidence it needs. This keeps the visual system ready for G4 without implying that analytics already exist.

## Reference use

Lucian component #039 informed the tab interaction pattern only. Its sample data and code were not copied. No new package or external design-system dependency was introduced.
