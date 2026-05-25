import type { ComponentChildren } from "preact";
import { useEffect } from "preact/hooks";
import "./modal.css";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ComponentChildren;
  footer?: ComponentChildren;
  children: ComponentChildren;
  width?: "sm" | "md" | "lg";
}

export function Modal({ open, onClose, title, footer, children, width = "md" }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div class="modal-backdrop" onClick={onClose} role="presentation">
      <div
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
