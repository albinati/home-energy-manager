import { Link, useLocation } from "wouter-preact";
import { ThemeToggle } from "./ThemeToggle";
import { AdminButton } from "./AdminButton";
import { role } from "../../lib/auth";

const tabs = [
  { href: "/", label: "Home" },
  { href: "/insights", label: "Insights" },
  // Journal + Settings are admin-only (they expose action-log + config).
  { href: "/report", label: "Journal", adminOnly: true },
  { href: "/settings", label: "Settings", adminOnly: true },
];

export function TopNav() {
  const [path] = useLocation();
  const isAdmin = role.value === "admin";
  const visible = tabs.filter((t) => !t.adminOnly || isAdmin);
  return (
    <header class="topnav">
      <div class="topnav-inner">
        <Link href="/" class="brand">
          <span class="brand-mark">H</span>
          <span>Home Energy Manager</span>
        </Link>
        <nav class="topnav-tabs" aria-label="Primary">
          {visible.map((t) => (
            <Link
              key={t.href}
              href={t.href}
              class={`topnav-tab${path === t.href ? " active" : ""}`}
            >
              {t.label}
            </Link>
          ))}
        </nav>
        <AdminButton />
        <ThemeToggle />
      </div>
    </header>
  );
}
