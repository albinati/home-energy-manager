// Signal-backed toast queue. The <ToastHost/> in app.tsx subscribes; everywhere
// else just imports and calls `toast.info(msg)` or `toast.error(msg)`.

import { signal } from "@preact/signals-core";

export type ToastKind = "info" | "success" | "error";

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
  detail?: string;
}

let nextId = 1;
export const toasts = signal<ToastItem[]>([]);

function push(kind: ToastKind, message: string, detail?: string): number {
  const id = nextId++;
  toasts.value = [...toasts.value, { id, kind, message, detail }];
  const ttl = kind === "error" ? 8000 : 4000;
  window.setTimeout(() => dismiss(id), ttl);
  return id;
}

export function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id);
}

export const toast = {
  info: (m: string, d?: string) => push("info", m, d),
  success: (m: string, d?: string) => push("success", m, d),
  error: (m: string, d?: string) => push("error", m, d),
};
