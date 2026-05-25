import type { ComponentChildren } from "preact";
import "./pill.css";

interface PillProps {
  children: ComponentChildren;
  tone?: "neutral" | "accent" | "ok" | "warn" | "bad" | "dim";
  icon?: ComponentChildren;
  title?: string;
}

export function Pill({ children, tone = "neutral", icon, title }: PillProps) {
  return (
    <span class={`pill pill--${tone}`} title={title}>
      {icon && <span class="pill-icon">{icon}</span>}
      {children}
    </span>
  );
}
