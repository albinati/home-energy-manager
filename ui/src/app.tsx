import { Route, Switch } from "wouter-preact";
import { TopNav } from "./components/shell/TopNav";
import { Footer } from "./components/shell/Footer";
import { ToastHost } from "./components/common/Toast";
import Landing from "./routes/landing";
import Plan from "./routes/plan";
import Settings from "./routes/settings";

export function App() {
  return (
    <div class="shell">
      <TopNav />
      <main class="shell-main">
        <Switch>
          <Route path="/" component={Landing} />
          <Route path="/plan" component={Plan} />
          <Route path="/settings" component={Settings} />
          <Route>
            <div class="page-padded">
              <h1>Not found</h1>
              <p>The page you're looking for doesn't exist in this app.</p>
            </div>
          </Route>
        </Switch>
      </main>
      <Footer />
      <ToastHost />
    </div>
  );
}
