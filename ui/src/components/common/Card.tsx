import type { ComponentChildren } from "preact";
import "./card.css";

interface CardProps {
  title?: ComponentChildren;
  subtitle?: ComponentChildren;
  action?: ComponentChildren;
  children: ComponentChildren;
  variant?: "default" | "elevated" | "subtle";
  pad?: "tight" | "default" | "loose";
  class?: string;
}

export function Card({
  title,
  subtitle,
  action,
  children,
  variant = "default",
  pad = "default",
  class: cls = "",
}: CardProps) {
  return (
    <section class={`card card--${variant} card--pad-${pad} ${cls}`}>
      {(title || action) && (
        <header class="card-head">
          <div>
            {title && <h3 class="card-title">{title}</h3>}
            {subtitle && <div class="card-subtitle">{subtitle}</div>}
          </div>
          {action && <div class="card-action">{action}</div>}
        </header>
      )}
      <div class="card-body">{children}</div>
    </section>
  );
}
