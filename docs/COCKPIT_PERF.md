# Cockpit load performance — diagnosis, what's done, what's left

Living notes on why the cockpit (`/`) was slow to populate and how we're
fixing it without overloading the small Hetzner box. Start here before
touching UI fetch fan-out or adding API caches.

## Server baseline (don't over-engineer for it)

Prod is a **Hetzner CAX11: 2 vCPU ARM64, 3.7 GB RAM**, shared with other
containers. At rest it is **idle** — the bottleneck has never been raw
resources:

| container | mem (limit) | role |
|---|---|---|
| `hem` | ~137 MB / **400 MB** | FastAPI + SQLite + APScheduler (single process) |
| `hem-ui` | ~3 MB / **64 MB** | nginx serving the SPA + reverse-proxying `/api` |
| `hem-quartz` | ~220 MB / **1 GB** | solar-forecast sidecar (lazy model) |
| `executa-bot` | ~58 MB | OpenClaw |

Host load average during a cockpit load: ~0.05. So **"make the server bigger"
is the wrong instinct** — the wins are in *not making it do redundant work*.

## The core constraint: one event loop, synchronous compute serialises

`hem` is a **single-process** FastAPI app. Any handler that does synchronous
CPU/SQLite work *on the event loop* blocks every other in-flight request for
its duration. The SPA fires ~12 API requests on first paint, so one ~1 s
synchronous handler doesn't cost 1 s — it makes the whole fan-out queue
head-to-tail.

### How to diagnose (reproducible)

1. **Headless network capture** against the funnel — look for a *wall* of
   responses all resolving at the same late timestamp (the tell-tale of
   loop blocking), vs. the fast ones that slipped through:
   ```
   $B viewport 1280x800; $B goto https://<host>:8443/; $B wait .powerflow-svg; $B network
   ```
2. **Per-endpoint isolated timing** direct on the box (bypass nginx/Tailscale),
   warm cache, sequential — reveals each handler's intrinsic cost:
   ```
   for ep in cockpit/now metrics pv/today ... ; do
     curl -s -o /dev/null -w "$ep %{time_total}s\n" http://127.0.0.1:8000/api/v1/$ep
   done
   ```
3. **Concurrency test** — fire two heavy ones at once; if both take ~2× their
   solo time, they're serialising on the loop (not truly async).
4. `docker stats --no-stream` during a refresh to confirm hem is the CPU spike.

### 2026-06-13 audit result

Isolated warm timings: the entire fast set (`cockpit/now` 17 ms, `timeline`
14 ms, `pv` 51 ms, `weather` 4 ms, …) summed to ~150 ms. The slow set:

| endpoint | warm time | note |
|---|---|---|
| `energy/monthly` ×6 (lifetime strip) | **0.84–2.75 s each (~10.4 s)** | uncached `compute_monthly_pnl` per call |
| `metrics` | **0.99 s** | synchronous PnL, on-loop, no result cache |

→ the above-the-fold wall was ~8.5 s, dominated by the decorative lifetime
footer re-running six PnL replays every load.

## What's done (Camada 1+2 — PR #557)

Both **reduce** hem CPU; neither touches the dispatch loop, the scheduler, or
the other containers.

- **`GET /api/v1/energy/lifetime`** — one cached aggregate replacing the six
  `/energy/monthly` calls. Sums solar / export / saved-vs-fixed across the
  active on-Agile months once, off the loop, behind a 1 h TTL
  (`LIFETIME_CACHE_TTL_SECONDS`). `LifetimeStrip.tsx` makes one deferred call.
- **`/metrics`** — body extracted to `_compute_metrics()`, served behind a 60 s
  TTL (`METRICS_CACHE_TTL_SECONDS`) and computed via `asyncio.to_thread`. Live
  SoC still comes from `/cockpit/now`, so a ~minute-stale figure here is fine.

### The in-process TTL cache idiom (reuse this)

Module-level dict + wall-clock TTL, mirroring `_period_insights_cache`
(introduced #507):

```python
_x_cache: dict[tuple, tuple[float, dict]] = {}   # or a single tuple for no-param endpoints
# in the handler: check (now - ts) < ttl → return; else compute via to_thread; store.
```

Each cache has a `*_CACHE_TTL_SECONDS` knob; **TTL=0 is the kill-switch** (always
recompute). There is deliberately **no single-flight lock** here — a cold-cache
burst can still double-compute; the TTL is the cheap win. If a handler ever
costs a *live vendor call*, use the single-flight `_cached_async` pattern in
`src/api/routers/status.py` instead, so client count can't amplify into quota.

## What's left (ranked by leverage)

- **Camada 3 — nginx micro-cache for viewer GETs.** `hem-ui` nginx currently
  does gzip + static `expires` but does **not** `proxy_cache` the API. A short
  (5–15 s) `proxy_cache` on `/api/v1/*`, **keyed to bypass when an
  `Authorization` header is present** (admin never cached) and respecting the
  per-endpoint `Cache-Control` (`_CACHE_CONTROL_MAX_AGE` in `main.py`), would
  collapse N tabs/clients to one backend hit per window — the real protection
  against "many open dashboards" on the small box. Costs a few MB in the 64 MB
  nginx container.
- **Camada 4 — reduce first-paint fan-out.** ~30 requests, ~12 hitting hem at
  t=0. Push more below-the-fold fetches behind `useAfterPaint` + idle, and/or
  add a single `/cockpit/bootstrap` aggregate the server assembles from warm
  caches so the first paint needs one round-trip, not a dozen.
- **Camada 5 — bundle.** `echarts` (~208 KB gzip) is the heaviest asset; already
  lazy + immutable-cached 7 d. Low priority; ensure hashed assets carry
  `Cache-Control: immutable` so repeat visits skip revalidation.

## Guard rails

- Never add a synchronous vendor HTTP call (Octopus/Fox/Daikin/Quartz) inline in
  an `async def` handler — wrap in `asyncio.to_thread`. (`/scheduler/status` was
  fixed this way in #555; it still does a live Octopus fetch under the hood.)
- Caches that front historical data (lifetime, closed-month PnL) can be aggressive
  (hours). Caches fronting live state (SoC, prices) stay short and the live value
  must also be reachable on a fast always-fresh endpoint (`/cockpit/now`).
- Measure before/after with the same headless `network` capture; target for the
  above-the-fold wall is **< 1 s**.
