import { Link, useLocation } from "wouter-preact";
import { ThemeToggle } from "./ThemeToggle";
import { AdminButton } from "./AdminButton";
import { PeriodNavigator } from "./PeriodNavigator";
import { Icon } from "../common/Icon";
import { role } from "../../lib/auth";

// Routes whose content follows the shared period signal — these get the
// compact period control in the chrome (redesign P4c). Other routes keep a
// plain bar (the spacers collapse the gap).
const periodRoutes = ["/", "/insights"];

// Redesign chrome: the brand IS the home link (no "Home" tab); routes are
// quiet borderless nav-links with a thin-line icon; Settings demotes to an
// icon-btn; theme toggle is an icon-btn (inside ThemeToggle).
export function TopNav() {
  const [path] = useLocation();
  const isAdmin = role.value === "admin";
  return (
    <header class="topnav">
      <div class="topnav-inner">
        <Link href="/" class="brand">
          <span class="brand-mark">H</span>
          <span class="brand-name">
            Home Energy Manager{path === "/" && <span class="brand-dim"> · cockpit</span>}
          </span>
        </Link>
        <span class="topnav-spacer" />
        {periodRoutes.includes(path) && <PeriodNavigator variant="chrome" />}
        <span class="topnav-spacer" />
        <nav class="topnav-tabs" aria-label="Primary">
          <Link href="/insights" class={`nav-link${path === "/insights" ? " active" : ""}`}>
            <Icon name="trend" size={15} />Insights
          </Link>
          {isAdmin && (
            <Link href="/report" class={`nav-link${path === "/report" ? " active" : ""}`}>
              <Icon name="schedule" size={15} />Journal
            </Link>
          )}
          {isAdmin && (
            <Link href="/settings" class={`icon-btn${path === "/settings" ? " active" : ""}`}
                  aria-label="Settings" title="Settings">
              <Icon name="settings" size={16} />
            </Link>
          )}
        </nav>
        <AdminButton />
        <ThemeToggle />
      </div>
    </header>
  );
}
