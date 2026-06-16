// Stale lazy-chunk recovery.
//
// After a UI deploy the hashed lazy-chunk filenames change (e.g.
// HeatingPlanWidget-B1w8BjzK.js → -D2apYkIO.js). A tab opened BEFORE the deploy
// still holds the old references, so a later lazy import 404s with "Failed to
// fetch dynamically imported module". The in-app "Retry" can't help — it re-runs
// the same dead URL. Recover transparently instead: on a chunk-load failure,
// full-reload ONCE to pull the fresh index.html + new hashes.
//
// Guarded by a short sessionStorage cooldown so a genuinely broken deploy (the
// new chunk also fails) can't reload-loop — after one attempt we fall through to
// the error UI.

const RELOAD_KEY = "hem:chunk-reload-at";
const COOLDOWN_MS = 15_000;

/** True when the error looks like a missing/failed dynamic-import chunk. */
export function isChunkLoadError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  return /dynamically imported module|Importing a module script failed|error loading dynamically imported|Failed to fetch dynamically/i.test(msg);
}

/** Reload once to recover from a stale-chunk error. Returns false when we're
 *  still in the cooldown (already tried recently) so the caller can show the
 *  error UI instead of looping. */
export function reloadForFreshChunks(): boolean {
  let last = 0;
  try { last = Number(sessionStorage.getItem(RELOAD_KEY) || 0); } catch { /* private mode */ }
  if (Date.now() - last < COOLDOWN_MS) return false;
  try { sessionStorage.setItem(RELOAD_KEY, String(Date.now())); } catch { /* ignore */ }
  window.location.reload();
  return true;
}

/** Wire global recovery for Vite's dynamic-import preload failures. Call once at
 *  boot. ``vite:preloadError`` fires when a lazy() import's chunk can't load;
 *  preventing default stops Vite rethrowing (which would crash to the boundary)
 *  and we reload to the fresh build. In cooldown we let it fall through. */
export function installChunkReload(): void {
  window.addEventListener("vite:preloadError", (e: Event) => {
    if (reloadForFreshChunks()) e.preventDefault();
  });
}
