import type { ComponentChildren } from "preact";
import type { TileText } from "./logic";

export function Tile({ icon, title, t }: { icon: ComponentChildren; title: string; t: TileText }) {
  return (
    <section class={`hs-tile hs-tile--${t.tone}`} aria-label={title}>
      <h2 class="hs-eyebrow">{icon}{title}</h2>
      <div class="hs-big">
        <span class="hs-num">{t.value ?? "—"}</span>
        {t.value != null && <span class="hs-unit">{t.unit}</span>}
      </div>
      {t.flow && <p class={`hs-flow hs-flow--${t.flowTone ?? "idle"}`}>{t.flow}</p>}
      <p class="hs-meaning">{t.meaning}</p>
      {t.next && <p class="hs-next">{t.next}</p>}
    </section>
  );
}
