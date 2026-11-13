# TrendScout design-system specification

**State:** Proposed extension of the existing visual system. Preserve current brand/tokens; do not implement in G0.

## Visual direction

Professional research workspace; warm light surfaces, restrained brown accent, cool navy dark surfaces, clear type hierarchy and low-noise separators. Avoid decorative gradients, glass effects, excessive borders and motion without task value. Reuse existing light/dark variables in `Frontend/public/styles.css` as the source of truth.

## Token roles

| Role | Existing token | Use |
| --- | --- | --- |
| Canvas/surface | `--canvas`, `--surface`, `--surface-raised`, `--surface-soft`, `--surface-accent` | App background, panels, menus, hover and selected areas |
| Text | `--ink-strong`, `--ink`, `--muted`, `--muted-soft` | Heading/body/meta; do not use muted alone for critical data |
| Brand | `--accent`, `--accent-strong`, `--accent-soft` | Primary action, link and selected state |
| Status | `--teal`, `--amber`, `--red`, `--green` and soft variants | Semantic status with icon and text, contrast-checked in both themes |
| Border/focus/elevation | `--line`, `--line-strong`, `--focus`, `--shadow` | Quiet separation, keyboard focus and restrained layering |

Proposed scales to validate against CSS: spacing 4/8/12/16/24/32/40 px; radius 4/8/12 px; type 12/14/16/20/24/32 px with 1.4–1.6 body line-height; 40–44 px minimum primary control height. These are targets, not current approved values.

## Components

- **Navigation:** task areas first; advanced views grouped. Active link uses shape/text, not color only. Mobile drawer supports `aria-expanded`, Escape and focus return.
- **Buttons:** one primary action per region; clear secondary/tertiary hierarchy; destructive action confirmed; disabled state explains why.
- **Inputs:** persistent label, visible focus and errors; search never triggers provider Fetch per keystroke.
- **Tabs:** semantic tablist/tab/tabpanel, arrow-key navigation and selected state. Dashboard has Overview/Analysis.
- **Tables:** announced sort direction; explicit page vs all-filtered selection; contextual empty/error/loading states; mobile alternative.
- **Cards:** group related facts without nested card-on-card; metadata rows for source/scope/time.
- **Status:** shared vocabulary; text plus icon. Separate adapter/config/approval/live/current-health/production axes.
- **Dialogs/drawers:** title, initial focus, Escape policy, focus containment/return and impact summary for destructive actions.
- **Tooltips/details:** essential meaning stays visible; support keyboard/touch; technical evidence may expand.
- **Charts:** axes/units, gaps for missing values, accessible summary/table; no illustrative product-page data.
- **Loading/empty/error:** stable skeleton; filter-remediation empty state; source-aware safe recovery; stale content stays labeled; reduced motion honored.
- **Toast/alerts:** accessible success/error; persistent notice when interpretation is affected.

## Responsive and accessibility targets

- Support 320 px upward across phone/tablet/desktop. Candidate breakpoints near 640/900/1200 px require layout validation.
- At narrow width, collapse navigation/filters intentionally; table scrolling is local to table, not page overflow.
- Target WCAG 2.2 AA for contrast, keyboard, focus, target size, labels, semantics, status announcement and chart alternatives; verify light and dark.
- Motion is brief and useful, never necessary to understand state, and reduced for `prefers-reduced-motion`.

## Iconography

Use shipped consistent icons or simple inline geometry. Provider identity comes from backend descriptors and text. Do not use provider marks to imply LIVE or generic social icons to imply support. Status is distinguishable without color.
