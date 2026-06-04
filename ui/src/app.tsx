import { Route, Switch } from "wouter-preact";
import { lazy, Suspense } from "preact/compat";
import { TopNav } from "./components/shell/TopNav";
import { Footer } from "./components/shell/Footer";
import { ToastHost } from "./components/common/Toast";
import { Spinner } from "./components/common/Spinner";
import Landing from "./routes/landing";

// Settings + Report + Insights are lazy — off the critical path (home is default).
const Settings = lazy(() => import("./routes/settings"));
const Report = lazy(() => import("./routes/report"));
const Insights = lazy(() => import("./routes/insights"));

export function App() {
  return (
    <div class="shell">
      <TopNav />
      <main class="shell-main">
        <Suspense fallback={<div class="page-padded"><Spinner label="Loading…" /></div>}>
          <Switch>
            <Route path="/" component={Landing} />
            <Route path="/insights" component={Insights} />
            <Route path="/report" component={Report} />
            <Route path="/settings" component={Settings} />
            <Route>
              <div class="page-padded">
                <h1>Not found</h1>
                <p>The page you're looking for doesn't exist in this app.</p>
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
