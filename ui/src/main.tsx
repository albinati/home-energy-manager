import { render } from "preact";
import { App } from "./app";
// Boot the theme system early so it applies the right class before the first
// paint (avoids a flash of wrong-theme).
import "./lib/theme";
import { applyMotionClass } from "./lib/motion";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/shell.css";

// Set the reduce-motion html class before first paint so the CSS animation
// gates match the motion preference (default: on, overriding the OS setting).
applyMotionClass();

const root = document.getElementById("app");
if (!root) throw new Error("#app missing from index.html");
render(<App />, root);
