import { render } from "preact";
import { App } from "./app";
// Boot the theme system early so it applies the right class before the first
// paint (avoids a flash of wrong-theme).
import "./lib/theme";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/shell.css";

const root = document.getElementById("app");
if (!root) throw new Error("#app missing from index.html");
render(<App />, root);
