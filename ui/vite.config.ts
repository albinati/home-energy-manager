import { defineConfig, loadEnv } from "vite";
import preact from "@preact/preset-vite";

// HEM SPA build config.
//
// Dev: `npm run dev` boots Vite on :5173. `/api` requests are proxied to the
// HEM API host pointed to by VITE_DEV_API_TARGET (default http://localhost:8000).
// During dev, `public/config.js` (gitignored) supplies window.__HEM_CONFIG__ with
// a bearer for the sim box; in prod, the nginx entrypoint writes config.js fresh
// at container start.
//
// Build: `npm run build` emits dist/ with hashed assets that nginx serves long-
// cached. BUILD_SHA (passed by the Dockerfile / CI) is baked into the bundle.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_DEV_API_TARGET || "http://localhost:8000";
  // Optional split: route specific paths to a SECOND backend (e.g. local box
  // running not-yet-deployed endpoints) while the rest hits VITE_DEV_API_TARGET.
  // Comma-separated path prefixes in VITE_DEV_LOCAL_PATHS → VITE_DEV_LOCAL_API.
  const localTarget = env.VITE_DEV_LOCAL_API || "http://localhost:8000";
  const localPaths = (env.VITE_DEV_LOCAL_PATHS || "")
    .split(",").map((p) => p.trim()).filter(Boolean);
  const proxy: Record<string, { target: string; changeOrigin: boolean }> = {};
  for (const p of localPaths) proxy[p] = { target: localTarget, changeOrigin: true };
  proxy["/api"] = { target: apiTarget, changeOrigin: true };

  return {
    plugins: [preact()],
    define: {
      __BUILD_SHA__: JSON.stringify(env.VITE_BUILD_SHA || "dev"),
    },
    server: {
      port: 5173,
      strictPort: true,
      proxy,
    },
    build: {
      outDir: "dist",
      assetsDir: "assets",
      target: "es2022",
      sourcemap: false,
      rollupOptions: {
        output: {
          manualChunks: {
            echarts: ["echarts"],
          },
        },
      },
    },
  };
});
