import { Icon } from "../../common/Icon";
import type { Chip } from "../../shell/AlertStrip";
import { AGORA_TITLE } from "./logic";

// One sentence. If anything is wrong the alert chips REPLACE it (same tones as
// the shell AlertStrip: bad / warn).
export function StatusLine({ chips, text }: { chips: Chip[]; text: string }) {
  if (chips.length > 0) {
    return (
      <p class="hs-status hs-status--alert" aria-live="polite">
        {chips.map((c) => (
          <span key={c.key} class={`alert-chip alert-chip--${c.tone}`} title={c.detail}>
            <Icon name="warn" size={12} />
            {c.label}
          </span>
        ))}
      </p>
    );
  }
  return (
    <p class="hs-status" aria-live="polite">
      <span class="hs-dot" aria-hidden="true" />
      {text}
    </p>
  );
}

// "Agora": live house balance, one row (wraps on phones).
export function AgoraLine({ parts, stale }: { parts: string[]; stale: string }) {
  if (parts.length === 0) return null;
  return (
    <p class="hs-agora" title={AGORA_TITLE}>
      Agora:{" "}
      {parts.map((t, i) => (
        <span key={t}>
          {i > 0 && " · "}
          <span class="hs-agora-i">{t}</span>
        </span>
      ))}
      {stale}
    </p>
  );
}
