import { Link, useLocation } from "wouter-preact";

// New SPA routes (wouter <Link> — client-side nav, no full reload).
const spaTabs = [
  { href: "/", label: "Home" },
  { href: "/cockpit", label: "Cockpit" },
  { href: "/forecast", label: "Forecast" },
  { href: "/settings", label: "Settings" },
];

// Legacy pages still served by nginx as static HTML. Plain <a> = full reload
// out of the SPA into the legacy bundle.
const legacyTabs = [
  { href: "/history", label: "History" },
  { href: "/insights", label: "Insights" },
  { href: "/workbench", label: "Workbench" },
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
          {spaTabs.map((t) => (
            <Link
              key={t.href}
              href={t.href}
              class={`topnav-tab${path === t.href ? " active" : ""}`}
            >
              {t.label}
            </Link>
          ))}
          {legacyTabs.map((t) => (
            <a key={t.href} href={t.href} class="topnav-tab legacy" title="Legacy page (full reload)">
              {t.label}
            </a>
          ))}
        </nav>
      </div>
    </header>
  );
}
