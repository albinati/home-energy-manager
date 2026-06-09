# HEM cockpit — design brief (for claude.ai/design)

A home-energy dashboard for a solar + battery + heat-pump house on Octopus Agile
(import) + Outgoing Agile (export). Dark theme, "Apple/Tesla" glanceable feel.
**Goal of the redesign:** make it cleaner / easier to read at a glance, keep all
the data below, suggest a better information hierarchy + visual treatment.

**Port target (don't design away from this):** Preact + TypeScript + **ECharts**
(real charts), a **12-column responsive widget grid**. Mockup can be plain
HTML/React/Tailwind — it gets translated back into this stack. Charts in the
snapshot are SVG approximations; in the real app they're interactive ECharts.

Companion file: `cockpit-snapshot.html` — the current layout with real data
frozen 2026-06-08.

---

## Structure (top → bottom)

### 0. Period navigator (sticky-ish, top)
- Granularity tabs: **Day / Week / Month / Year** (default Day).
- Prev/next stepper + the period label ("Today · 8 Jun 2026").
- Drives the Hero + Generation + Consumption (NOT the live widgets).

### 1. Hero (full width)
The "how am I doing" headline.
- **Big number:** the period's net bill so far — `£1.26` today.
- **Key stat:** `4.0 kWh da rede hoje` (grid import — the billed kWh) · `standing £0.59/dia`.
- **vs fixed:** `Gastou +£0.03 hoje vs British Gas Fixed v58` (green if saved, red if behind).
- **Target line:** `🎯 meta: import médio ≤ 18.7p · hoje 16.9p ✓` — the avg import price needed to beat the fixed tariff (Agile loses on standing, must win on unit price).
- **Live-now strip:** `import 15.4p · export 9.0p · battery 48%` (always current).
- **Right panel:** today's import-price sparkline coloured by tier (paid/cheap/standard/peak) + now-marker.
- **Lifetime strip:** solar produced, exported, £ saved vs fixed (on-Agile months).

### 2. Row — Live power | Live heating (50/50)
**Live power** (always "now"):
- Power-flow schematic: solar → grid → house + battery (SoC 48%).
- Import rate `15.4p` (4.0 kWh · £0.67) + Export rate `9.0p` (0.0 kWh).
- "Next:" the soonest battery + tank action.

**Live heating** (always "now") — gauges + the heating-plan timeline merged:
- Gauges: **Tank** `50°C` (target 45, on/off) · **Outdoor** `16°C` · **LWT offset** `+3°` · Heating today `2.1 kWh est`.
- **Heating-plan chart (today):** outdoor temp, weather-curve LWT, radiator LWT (the commanded line), tank target (step), import-price (right axis); tariff bands shaded (negative blue / peak amber).

### 3. Row — Plan | Weather (50/50)
**Plan** — the committed dispatch, forward-looking, 4 groups of chips:
- **Battery:** upcoming forced windows (Force charge / discharge / drain) + Fox mode.
- **Heating:** radiator LWT offset windows (Boost +N° / Setback −N°).
- **Tank:** DHW schedule (Warmup/Setback/Boost + time + target °C).
- **Appliances:** per machine — running / scheduled HH:MM–HH:MM · price / next window.
- Footer: LP run id + plan date.

**Weather:**
- Current temp `14°`, condition, H/L, cloud %.
- **Solar expected · rest of day** `6.2 kWh` (forward sum from now).
- Today's solar curve (sunrise → peak → sunset) + now-marker.

### 4. Generation (full width) — period-synced
- Headline: `14.2 kWh solar esperado hoje` · `0.0 kWh export`.
- **Chart (day):** solar plan (dashed) vs actual (area), grid export, and the
  **export price** line on the right axis (Outgoing Agile curve — green dashed).
  Week/month/year → daily solar + export bars.

### 5. Consumption (full width) — period-synced
- Headline: `9.6 kWh consumido so far today`.
- **Chart (day):** stacked composition (base + appliances + heat-pump areas) +
  load forecast (dashed) + **import price** on the right axis (red dashed) +
  tariff bands (paid/cheap/peak). Week/month/year → daily load bars + Daikin heating/tank lines.

> Generation + Consumption are **stacked full-width** and share an identical
> x-axis (00:00–23:30) so a given time reads straight down the screen.

---

## Design tokens (dark theme — keep the palette)
```
bg #0b0f17 · card #111827 · card-2 #1f2937 · border #374151
text #f3f4f6 · dim #9ca3af · mute #6b7280
accent #3b82f6 · ok/cheap/export #10b981 · warn/peak #f59e0b · bad/import #ef4444
neg-price #2563eb · pv #fbbf24 · grid #60a5fa · house #c084fc
radius 10/16px · font: system-ui, tabular-nums
```
A light theme exists too (mirrors these). Spacing scale 0.25→3rem.

## Constraints / notes for the designer
- **Keep every data point above** — this is an info-dense control surface, not marketing.
- Colour is **semantic**: import=red, export=green, solar=yellow, battery/cheap=green, peak=amber, paid/negative=blue, heating=purple/orange. Don't repurpose.
- There are **NO manual controls** (climate/tank control removed — HEM/Daikin drive it). It's read-only + glanceable.
- The 4 timeline charts (heating, generation, consumption + the hero sparkline) are real ECharts; redesign their *framing/composition*, not the chart engine.
- Mobile: the 50/50 rows stack to full-width; the full-width charts stay full-width.

## What to return
An interactive **HTML/React Artifact** mockup of the redesigned cockpit (fake data
fine) + a short note on the hierarchy decisions. I (Claude Code) port it into the
real Preact + ECharts components and wire the live endpoints.
