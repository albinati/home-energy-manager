import "./spinner.css";

interface SpinnerProps {
  label?: string;
  size?: "sm" | "md";
}

export function Spinner({ label, size = "md" }: SpinnerProps) {
  return (
    <div class={`spinner spinner--${size}`} role="status">
      <div class="spinner-ring" aria-hidden="true" />
      {label && <span class="spinner-label">{label}</span>}
    </div>
  );
}
