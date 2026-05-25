import { useComputed } from "@preact/signals";
import { toasts, dismiss } from "../../lib/toast";
import "./toast.css";

export function ToastHost() {
  const items = useComputed(() => toasts.value);
  return (
    <div class="toast-host" role="status" aria-live="polite">
      {items.value.map((t) => (
        <div key={t.id} class={`toast toast--${t.kind}`}>
          <div class="toast-msg">{t.message}</div>
          {t.detail && <div class="toast-detail">{t.detail}</div>}
          <button class="toast-close" onClick={() => dismiss(t.id)} aria-label="Dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
