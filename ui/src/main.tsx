import { render } from "preact";
import { App } from "./app";
// Boot the theme system early so it applies the right class before the first
// paint (avoids a flash of wrong-theme).
import "./lib/theme";
import { applyMotionClass } from "./lib/motion";
import { installChunkReload } from "./lib/chunkReload";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/shell.css";

// Set the reduce-motion html class before first paint so the CSS animation
// gates match the motion preference (default: on, overriding the OS setting).
applyMotionClass();
// Recover transparently when a post-deploy stale lazy-chunk fails to load.
installChunkReload();

const root = document.getElementById("app");
if (!root) throw new Error("#app missing from index.html");
render(<App />, root);
