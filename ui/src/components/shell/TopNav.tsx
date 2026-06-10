import { Link, useLocation } from "wouter-preact";
import { ThemeToggle } from "./ThemeToggle";
import { AdminButton } from "./AdminButton";
import { PeriodNavigator } from "./PeriodNavigator";
import { role } from "../../lib/auth";

const tabs = [
  { href: "/", label: "Home" },
  { href: "/insights", label: "Insights" },
  // Journal + Settings are admin-only (they expose action-log + config).
  { href: "/report", label: "Journal", adminOnly: true },
  { href: "/settings", label: "Settings", adminOnly: true },
];

// Routes whose content follows the shared period signal — these get the
// compact period control in the chrome (redesign P4c). Other routes keep a
// plain bar (the spacers collapse the gap).
const periodRoutes = ["/", "/insights"];

export function TopNav() {
  const [path] = useLocation();
  const isAdmin = role.value === "admin";
  const visible = tabs.filter((t) => !t.adminOnly || isAdmin);
  return (
    <header class="topnav">
      <div class="topnav-inner">
        <Link href="/" class="brand">
          <span class="brand-mark">H</span>
          <span class="brand-name">Home Energy Manager</span>
        </Link>
        <span class="topnav-spacer" />
        {periodRoutes.includes(path) && <PeriodNavigator variant="chrome" />}
        <span class="topnav-spacer" />
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
