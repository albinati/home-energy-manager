import type { ComponentChildren } from "preact";
import { Icon } from "./Icon";
import { useErrorBoundary } from "preact/hooks";
import "./widget-boundary.css";

interface WidgetBoundaryProps {
  label?: string;
  children: ComponentChildren;
}

// Isolates a widget's render so one bad API payload degrades a single tile
// instead of unmounting the whole dashboard (Preact unwinds to the nearest
// boundary on a thrown render). Each Widget body is wrapped in one of these.
export function WidgetBoundary({ label, children }: WidgetBoundaryProps) {
  const [error, resetError] = useErrorBoundary();

  if (error) {
    return (
      <div class="widget-boundary-error" role="alert">
        <div class="widget-boundary-error-icon" aria-hidden="true"><Icon name="warn" size={18} /></div>
        <div class="widget-boundary-error-body">
          <div class="widget-boundary-error-title">
            {label ? `${label} couldn't render` : "This widget couldn't render"}
          </div>
          <div class="widget-boundary-error-detail">
            {error instanceof Error ? error.message : String(error)}
          </div>
          <button class="widget-boundary-error-retry" onClick={() => resetError()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
