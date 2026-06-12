import { Link, Route, Switch } from "wouter-preact";
import { Icon } from "./components/common/Icon";
import { lazy, Suspense } from "preact/compat";
import { useEffect } from "preact/hooks";
import type { ComponentType } from "preact";
import { TopNav } from "./components/shell/TopNav";
import { Footer } from "./components/shell/Footer";
import { ToastHost } from "./components/common/Toast";
import { Spinner } from "./components/common/Spinner";
import Landing from "./routes/landing";
import { role, refreshRole } from "./lib/auth";

// Settings + Report + Insights are lazy — off the critical path (home is default).
const Settings = lazy(() => import("./routes/settings"));
const Report = lazy(() => import("./routes/report"));
const Insights = lazy(() => import("./routes/insights"));

/** Render an admin-only route; viewers get a locked panel instead of the page
 *  (defense for a direct URL — the API also rejects the underlying calls). */
function AdminOnlyRoute({ component: C }: { component: ComponentType }) {
  if (role.value === "admin") return <C />;
  return (
    <div class="page-padded admin-locked">
      <h1><Icon name="lock" size={20} /> Admin required</h1>
      <p>This page is only available to admins. Use the <strong>Admin</strong> button
        in the top bar to unlock with your secret.</p>
    </div>
  );
}

export function App() {
  // Confirm our role with the server on boot (a stored admin token may have
  // been rotated → silently drops to viewer).
  useEffect(() => { refreshRole(); }, []);
  return (
    <div class="shell">
      <TopNav />
      <main class="shell-main">
        <Suspense fallback={<div class="page-padded"><Spinner label="Loading…" /></div>}>
          <Switch>
            <Route path="/" component={Landing} />
            <Route path="/insights" component={Insights} />
            <Route path="/report">{() => <AdminOnlyRoute component={Report} />}</Route>
            <Route path="/settings">{() => <AdminOnlyRoute component={Settings} />}</Route>
            <Route>
              <div class="page-padded not-found">
                <h1>Not found</h1>
                <p>The page you're looking for doesn't exist in this app. The
                  old per-section routes (heating, history, forecast) merged
                  into the cockpit.</p>
                <p class="not-found-links">
                  <Link href="/" class="nav-link"><Icon name="house" size={15} />Cockpit</Link>
                  <Link href="/insights" class="nav-link"><Icon name="trend" size={15} />Insights</Link>
                </p>
              </div>
            </Route>
          </Switch>
        </Suspense>
      </main>
      <Footer />
      <ToastHost />
    </div>
  );
}
