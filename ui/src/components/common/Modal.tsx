import type { ComponentChildren } from "preact";
import { useEffect, useRef } from "preact/hooks";
import "./modal.css";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ComponentChildren;
  footer?: ComponentChildren;
  children: ComponentChildren;
  width?: "sm" | "md" | "lg";
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), ' +
  'select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({ open, onClose, title, footer, children, width = "md" }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  // The element that had focus before the dialog opened — restored on close.
  const restoreRef = useRef<HTMLElement | null>(null);
  // Hold onClose in a ref so the focus effect can depend on [open] alone and
  // never re-run (re-grabbing focus) just because the parent re-rendered.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    restoreRef.current = (document.activeElement as HTMLElement | null) ?? null;

    const focusables = (): HTMLElement[] =>
      dialog ? Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)) : [];

    // Move focus into the dialog (first focusable, else the dialog itself).
    (focusables()[0] ?? dialog)?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !dialog) return;
      // Focus trap: keep Tab / Shift+Tab cycling inside the dialog.
      const items = focusables();
      if (items.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !dialog.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !dialog.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Return focus to whatever opened the dialog (the trigger).
      restoreRef.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  return (
    <div class="modal-backdrop" onClick={onClose} role="presentation">
      <div
        ref={dialogRef}
        tabIndex={-1}
        class={`modal modal--${width}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        {title && (
          <header class="modal-head">
            <h2 class="modal-title">{title}</h2>
            <button class="modal-close" onClick={onClose} aria-label="Close">×</button>
          </header>
        )}
        <div class="modal-body">{children}</div>
        {footer && <footer class="modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}
