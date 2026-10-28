import { useEffect, useState } from "preact/hooks";
import { Link, useLocation } from "wouter-preact";
import { ThemeToggle } from "./ThemeToggle";
import { AdminButton } from "./AdminButton";
import { PeriodNavigator } from "./PeriodNavigator";
import { Icon } from "../common/Icon";
import { role } from "../../lib/auth";

// Period control only where the page follows it (Insights). Home has none (#879).
const periodRoutes = ["/insights"];

/** Local wall clock, HH:MM, ticking every 20 s. */
function Clock() {
  const fmt = () => new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", hour12: false });
  const [t, setT] = useState(fmt);
  useEffect(() => {
    const id = setInterval(() => setT(fmt()), 20_000);
    return () => clearInterval(id);
  }, []);
  return <span class="nav-clock" aria-label="Hora local">{t}</span>;
}

// Chrome (#879): brand · Casa · Insights · Ajustes (admin) · clock. The brand
// is also the home link; on phones the links collapse into the brand.
export function TopNav() {
  const [path] = useLocation();
  const isAdmin = role.value === "admin";
  return (
    <header class="topnav">
      <div class="topnav-inner">
        <Link href="/" class="brand">
          <span class="brand-mark">H</span>
          <span class="brand-name">HEM</span>
        </Link>
        <nav class="topnav-tabs" aria-label="Primary">
          <Link href="/" class={`nav-link nav-link--text${path === "/" ? " active" : ""}`}>Casa</Link>
          <Link href="/insights" title="Insights" class={`nav-link nav-link--text${path === "/insights" ? " active" : ""}`}>
            Insights
          </Link>
          {isAdmin && (
            <Link href="/report" title="Journal" class={`nav-link nav-link--text${path === "/report" ? " active" : ""}`}>
              Journal
            </Link>
          )}
          {isAdmin && (
            <Link href="/settings" title="Ajustes" class={`nav-link nav-link--text${path === "/settings" ? " active" : ""}`}>
              <Icon name="settings" size={14} />Ajustes
            </Link>
          )}
        </nav>
        <span class="topnav-spacer" />
        {periodRoutes.includes(path) && <PeriodNavigator variant="chrome" />}
        <Clock />
        <AdminButton />
        <ThemeToggle />
      </div>
    </header>
  );
}
