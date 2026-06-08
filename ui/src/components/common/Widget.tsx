import type { ComponentChildren } from "preact";
import { WidgetBoundary } from "./WidgetBoundary";
import "./widget.css";

export type WidgetSize = "medium" | "half" | "large" | "wide";
export type WidgetTone = "default" | "power" | "tariff" | "thermal" | "savings" | "plan" | "coming";

interface WidgetProps {
  title: ComponentChildren;
  icon?: ComponentChildren;
  badge?: ComponentChildren;
  action?: ComponentChildren;
  size?: WidgetSize;
  tone?: WidgetTone;
  children: ComponentChildren;
  class?: string;
}

// Visual primitive for the home + plan grids. Each widget has a consistent
// header (icon + title + badge slot) and an accent line on the left edge
// coloured by `tone` so domains read at a glance. Size controls grid span
// on desktop (mobile always stacks to full width).
export function Widget({
  title,
  icon,
  badge,
  action,
  size = "medium",
  tone = "default",
  children,
  class: cls = "",
}: WidgetProps) {
  // The title is usually a plain string — use it to label the error fallback.
  const label = typeof title === "string" ? title : undefined;
  return (
    <section class={`widget widget--${size} widget--tone-${tone} ${cls}`}>
      <header class="widget-header">
        <div class="widget-header-title">
          {icon && <span class="widget-header-icon" aria-hidden="true">{icon}</span>}
          {title}
        </div>
        <div class="widget-header-meta">
          {badge && <span class="widget-header-badge">{badge}</span>}
          {action}
        </div>
      </header>
      <div class="widget-body">
        <WidgetBoundary label={label}>{children}</WidgetBoundary>
      </div>
    </section>
  );
}
