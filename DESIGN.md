# Design System — Home Energy Manager (HEM) cockpit

Source of truth for all visual + UI decisions. Read this before any UI change.
Token values are authoritative in `ui/src/styles/tokens.css`; this file is the
rationale + the rules. When they disagree, fix the drift, don't fork the values.

## Product Context
- **What this is:** real-time ops console for a UK residential solar + battery +
  heat-pump system on half-hourly Octopus Agile tariffs. The UI monitors a MILP
  solver's committed plan vs. live hardware execution.
- **Who it's for:** the home's owner/operator. Viewer-by-default (shareable,
  read-only); admin unlocks controls with a token.
- **Project type:** information-dense ops console / control surface. NOT marketing.
- **Stack:** Preact 10 + wouter, Vite 5 + TypeScript (strict), ECharts 5
  (lazy chunk), `@preact/signals`, plain CSS + custom properties (no Tailwind,
  no CSS-in-JS). Served by nginx in a separate `hem-ui` container.
- **Routes:** `/` cockpit, `/insights` fair-tariff comparison, `/report`
  activity journal (admin), `/settings` runtime editor (admin).

## Aesthetic Direction
- **Direction:** Apple/Tesla glanceable — deep-dark, borderless, monochrome-first,
  data-dense. Hierarchy from size + weight + elevation, not color or chrome.
- **Decoration level:** intentional. Cards float on a two-layer shadow + specular
  sheen, not borders or accent edges. Subtle ambient background drift.
- **Mood:** serious instrument for a real system. Honest, quiet, fast.
- **Performance is a design constraint:** above-the-fold load ~0.2s on a 2-vCPU /
  4GB ARM box. Any visual choice that costs that budget is wrong by default.

## Typography (DELIBERATE: system fonts)
- **Stack:** `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif`.
- **Mono:** `ui-monospace, "SF Mono", Menlo, Consolas, monospace` (timestamps, code).
- **Why system, not a custom typeface:** zero font-load cost, native rendering,
  fits the 0.2s budget and the "instrument, not brochure" posture. The 2026-06-16
  audit flagged system fonts as the one "AI tell" — **accepted and overruled**:
  on this hardware, for a glanceable ops console, perf + native feel win. Revisit
  only if the perf budget changes.
- **Numbers:** `font-variant-numeric: tabular-nums` on all focal/data numbers.
- **Type scale (tokens):** `--font-2xs 0.68rem`, `--font-xs 0.75`, `--font-sm 0.875`,
  `--font-md 1rem` (base), `--font-lg 1.125`, `--font-xl 1.5`, `--font-2xl 2`,
  `--font-display 3.25rem` (one focal number per card),
  `--font-hero 4.5rem` (single anchor number per surface).
- **Weights:** 400 body, 500 nav/labels, 600 section titles/eyebrows/pills,
  700 headings + focal numbers.
- **Eyebrow pattern:** `--font-2xs`, weight 700, `text-transform: uppercase`,
  `letter-spacing: var(--tracking-eyebrow)` (0.12em). Used for all section labels.
- **Rule:** no off-scale `font-size` literals. If a size is missing, add a token.

## Color
- **Approach:** restrained + strictly semantic. Color is meaning, never decoration.
- **Surfaces (dark, default):** `--bg #090b11` → `--bg-card #141926` →
  `--bg-card-2 #1c2330` → `--bg-card-3 #29313f` (true elevation ladder, not
  lightness inversion). `--border #2b3340`, `--border-strong #3a4350`.
- **Text:** `--text #f3f4f6`, `--text-dim #9ca3af`, `--text-mute #838b98`
  (lightened from #6b7280 to clear WCAG AA on cards).
- **Brand/state:** `--accent #3b82f6` (blue = interaction signal only),
  `--ok #10b981`, `--warn #f59e0b`, `--bad #ef4444`.
- **Tariff bands (same in both themes for legibility):** `--neg-price #2563eb`,
  `--cheap #10b981`, `--standard #6b7280`, `--peak #f59e0b`,
  `--peak-export #ef4444`, `--neg #38bdf8` (paid-to-import).
- **Energy domains (FIXED — never repurpose):** `--pv #fbbf24` (solar),
  `--batt #10b981`, `--grid #60a5fa`, `--house #c084fc`, `--import #ef4444`,
  `--export #10b981`, `--thermal #fb923c` (heating/tank).
- **Light theme:** separately hand-tuned in `tokens.css` (`.theme-light`); darker
  brand/state values for contrast on white. `color-scheme` is set per theme.
- **Rule:** no `var(--x, #literal)` fallbacks. Every color is a defined token.

## Spacing
- **Base unit:** 4px. Tokens `--space-1 4px` … `--space-7 48px`
  (4, 8, 12, 16, 24, 32, 48), plus `--space-0 2px` — the deliberate half-step
  for chip/badge vertical rhythm (added 2026-07-02; below it is a hairline,
  not spacing). Used 240+ times — keep it that way.
- **Density:** comfortable-to-compact; this is a dashboard, not a landing page.
- **Rule:** no ad-hoc spacing literals. The 2026-06-16 audit found ~110; the
  2026-07-02 consolidation routed them through the tokens (gaps + paddings) —
  the survivors are optical alignment values annotated in place.

## Layout
- **Approach:** grid-disciplined. 12-column widget grid, `gap var(--space-3)` (12px).
- **Widget spans:** mobile = all `span 12`; ≥720px medium/half → 6; ≥1024px
  medium → 4, half → 6, large → 8, wide → 12.
- **Max content widths:** home `1200px`, settings `980px`. TopNav full-width sticky.
- **Cards (borderless):** specular sheen + inset highlight + faint hairline
  (`rgba(255,255,255,0.045)` dark / `rgba(15,23,42,0.06)` light) + two-layer shadow.
  No grey frame, no colored accent edge. Domain identity reads from the header
  icon + the focal-number tint, never the card border.
- **Radius:** `--radius-sm 6px` (buttons/inputs), `--radius 10px` (modals/tabs),
  `--radius-lg 18px` (cards/hero), `--radius-pill 999px` (pills/segmented controls).
- **Hover:** elevation lift (translateY -2px) + stronger shadow. Never a color flip.
- **Focus:** 2px solid outline, 2px offset (global `:focus-visible`).
- **Mobile rule (from brief):** 50/50 rows stack to full-width; full-width charts
  stay full-width.

## Motion
- **Approach:** intentional, choreographed from shared tokens. Default ON, with an
  in-app reduce-motion toggle that overrides the OS (`--*` collapse to 0.001ms).
- **Tokens:** `--dur-enter 420ms`, `--stagger-step 40ms`, and the sub-scale
  `--dur-fast 140ms` (hovers, colour flips) / `--dur-med 220ms` (small moves,
  fades) / `--dur-slow 700ms` (focal-number settles, big fills);
  `--ease-entrance cubic-bezier(0.22,1,0.36,1)` (card rise),
  `--ease-lock cubic-bezier(0.34,1.3,0.64,1)` (~2% focal-number settle overshoot).
- **Signature motions:** `widget-rise` (staggered entrance), `live-pulse` (the one
  continuous loop — live dots), modal/toast/backdrop entrances, ambient
  `body-bg-drift` (60s), skeleton shimmer.
- **Rule:** durations come from the token scale. The 2026-07-02 consolidation
  converted ~52 ad-hoc values; the only literals left are the ambient loops
  (`body-bg-drift 60s`, skeleton shimmer 1.4s, live-pulse) — keep it that way.

## Effects
- `--shadow: 0 1px 1px /50%, 0 12px 32px /50%` (dark); `--shadow-lg` larger.
- `--glow-accent: 0 0 16px rgba(59,130,246,0.18)` — accent glow is dialed back;
  blue signals interaction, not ambient decoration.

## Non-Negotiables
1. **Deep-dark default**, light theme separately tuned. Elevation ladder, not inversion.
2. **Borderless** — depth from shadow + specular, never frame borders or accent edges.
3. **No emoji** — all icons are inline SVG. (Audit found stray sun glyph / ◉ markers;
   keep purging them.)
4. **Semantic color is fixed** — the energy-domain palette above never gets
   repurposed for decoration.
5. **Info-dense, not marketing** — no hero happy-talk, no "Built for X" copy, no
   3-column icon-circle feature grids, no purple gradients, no centered-everything.
6. **System fonts** — deliberate; see Typography.
7. **Performance budget (~0.2s above-fold)** trumps visual flourish.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-06-08 | Cockpit redesign (Claude Design handoff): deep-dark elevation ladder, borderless specular cards, two-layer Tesla soft-depth, accent glow dialed back | `ui/design/cockpit-brief.md`; replace flat cards + colored accent edges |
| 2026-06-16 | `--text-mute` #6b7280 → #838b98 | Lift small-label contrast to ≈5.1:1 (WCAG AA) on cards |
| 2026-06-16 | `--thermal`, `--neg` promoted from `var(--x, literal)` fallbacks to real tokens | Kill silent hardcoded colors / fallback drift |
| 2026-06-17 | System-ui font stack kept (audit AI-tell flag overruled) | Perf + native feel on 2-vCPU box for a glanceable ops console |
| 2026-06-17 | DESIGN.md adopted as canonical spec | Stop token/scale drift; give QA something to check against |
| 2026-07-02 | `--cool` (chart-ramp cool end) + `--text-on-accent` tokens; every `var(--x, #literal)` fallback stripped (#621) | 7 fallbacks carried WRONG colors (amber/cyan mixups); light theme gained missing `--neg`/`--thermal`/`--cool` |
| 2026-07-02 | Motion sub-scale `--dur-fast/med/slow`; `--space-0 2px`; breakpoint scale documented (#595) | Converge the 52 ad-hoc durations + chip micro-spacing on tokens |

## Gap status (2026-07-02 re-audit — was "Open Gaps", 2026-06-16)
All gaps re-verified against source on 2026-07-02 (4-sweep conformity audit):
- **CLOSED** F-001 headings (#601 + #621 — cockpit outline h1→h2→h3 complete),
  F-002 chart alt-text, F-003 mobile overflow, F-004 modal focus trap,
  F-005 phantom tokens, F-006 contrast, F-007 settings aria-labels,
  F-014 color-scheme (#591/#592/#594), F-012 emoji purge (verified clean).
- **CLOSED** F-008 touch targets (#621): compact chrome controls keep their
  visual size; a `pointer: coarse` centred overlay extends every hit box to
  ≥44px (see base.css).
- **CLOSED** F-009 motion + F-011 literals (#595 consolidation, 2026-07-02):
  durations/fonts/gaps/paddings/radius routed through tokens (~170
  conversions). Residual literals are deliberate: em-relative sizes, ambient
  loop timings, hairlines, optical chart alignment, and a handful of
  sub-token badge sizes (8–9px, 27px) awaiting a design decision.
- **DOCUMENTED** F-010 breakpoints: canonical scale recorded in tokens.css
  (430 / 640 / 720 / 960·961 pair / 1024 / 1180); off-scale @media widths are
  exceptions that must justify themselves at the site.
