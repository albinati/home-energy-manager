import { Route, Switch } from "wouter-preact";
import { lazy, Suspense } from "preact/compat";
import { TopNav } from "./components/shell/TopNav";
import { Footer } from "./components/shell/Footer";
import { ToastHost } from "./components/common/Toast";
import { Spinner } from "./components/common/Spinner";
import Landing from "./routes/landing";

// Plan + Settings are lazy: they're off the critical path (home is the
// default route) and Plan pulls in the echarts chunk via its charts. Lazy
// here means a visitor who never opens /plan never downloads echarts.
const Plan = lazy(() => import("./routes/plan"));
const Settings = lazy(() => import("./routes/settings"));
const Workbench = lazy(() => import("./routes/workbench"));

export function App() {
  return (
    <div class="shell">
      <TopNav />
      <main class="shell-main">
        <Suspense fallback={<div class="page-padded"><Spinner label="Loading…" /></div>}>
          <Switch>
            <Route path="/" component={Landing} />
            <Route path="/plan" component={Plan} />
            <Route path="/settings" component={Settings} />
            <Route path="/workbench" component={Workbench} />
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
