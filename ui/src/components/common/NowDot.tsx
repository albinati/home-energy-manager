// The "now" marker used in chart legends/hints. Replaces the ◉ text glyph
// (no text-glyph markers per the design rules) with a small accent dot that
// matches the on-chart now indicator.
export function NowDot() {
  return (
    <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true" style="vertical-align:middle">
      <circle cx="4" cy="4" r="4" fill="var(--accent)" />
    </svg>
  );
}
