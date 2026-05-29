import { Link, useLocation } from "wouter-preact";
import { ThemeToggle } from "./ThemeToggle";
import { MotionToggle } from "./MotionToggle";

const tabs = [
  { href: "/", label: "Home" },
  { href: "/settings", label: "Settings" },
];

export function TopNav() {
  const [path] = useLocation();
  return (
    <header class="topnav">
      <div class="topnav-inner">
        <Link href="/" class="brand">
          <span class="brand-mark">H</span>
          <span>Home Energy Manager</span>
        </Link>
        <nav class="topnav-tabs" aria-label="Primary">
          {tabs.map((t) => (
            <Link
              key={t.href}
              href={t.href}
              class={`topnav-tab${path === t.href ? " active" : ""}`}
            >
              {t.label}
            </Link>
          ))}
        </nav>
        <MotionToggle />
        <ThemeToggle />
      </div>
    </header>
  );
}
