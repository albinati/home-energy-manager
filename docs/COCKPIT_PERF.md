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

## Camada 3 — nginx viewer micro-cache (PR #558, done)

`hem-ui` nginx now `proxy_cache`s viewer GET `/api/v1/*` for 10 s
(`ui/conf/api-cache.conf.template` http-context zone + the `/api/` location
policy). N open tabs / aggressive polls collapse to **one hem hit per window**.
Correctness guarantees worth remembering before you touch it:

- **Admin never cached, either direction.** Admin requests carry
  `Authorization: Bearer` → `proxy_cache_bypass` + `proxy_no_cache
  $http_authorization` (never served from cache, never stored).
- **Admin data can't leak to viewers.** A viewer (no token) hitting an
  `admin_read_prefixes` path gets a **401** from `ApiV1RoleAuth`, and
  `proxy_cache_valid 200 10s` caches **only 200s** — so a forbidden response
  never enters the cache. (If you ever make a viewer-forbidden path return
  200-with-error-body, this breaks — keep them 401.)
- **GET/HEAD only** (writes are POST → never cached). `proxy_cache_lock on` =
  single-flight so a cold-cache burst is one fill. Upstream `Cache-Control`
  is ignored on this shared edge (uniform 10 s TTL governs the anonymous,
  identical viewer responses). `X-Cache-Status` header exposes HIT/MISS/BYPASS.

Also shipped with it: `_prime_lifetime_cache` warms `_lifetime_cache` ~5 s after
boot (delayed, guarded, quota-neutral) so the first post-restart visitor skips
the ~14 s cold rollup.

## Where we landed (post Camada 1+2+3) — measured 2026-06-13

Headless full-page load against the funnel, after all three layers deployed
(`b3b70cd`): **25 API requests, all completing inside a 233 ms window**
(slowest single request 134 ms), hero £ populated. The original ~8.5 s wall is
gone — a **~36× improvement**. Each above-the-fold endpoint is <150 ms and the
fan-out runs in ~4 browser concurrency waves.

## What's left — evaluated and DEFERRED (don't over-build the small box)

- **Camada 4 — `/cockpit/bootstrap` aggregate: NOT worth building right now.**
  The rationale was "collapse the ~12-request first-paint fan-out into one
  round-trip." But the bottleneck was never the request *count* — it was the
  serialised synchronous compute, which Camada 1+2 fixed. With that gone, the
  25 requests already finish in 233 ms in parallel; a bootstrap endpoint would
  shave maybe one round-trip while adding a new backend surface (assembly,
  coupling, a cache to invalidate) to a 2-vCPU box we're deliberately keeping
  lean. Revisit only if a future page genuinely needs a single-payload first
  paint, or if mobile/high-latency testing shows the wave count hurting.
  - The *cheap* version, if ever wanted: gate the below-the-fold `useFetch`
    calls in `landing.tsx` (heating gauges, appliances, quotas) behind the
    existing `useAfterPaint` so the critical above-fold set goes out first.
    Zero new endpoints — but marginal now that every endpoint is <150 ms and
    the micro-cache absorbs repeats.
- **Camada 5 — bundle: low value, deferred.** `echarts` (~208 KB gzip) is the
  heaviest asset but is lazy-loaded + immutable-cached 7 d, and the page is
  already fast. Not worth a chart-library swap.

**Bottom line:** the cockpit-perf work is effectively complete. The serialised
compute (the real cause) is fixed, repeats are edge-cached, and the cold strip
is primed. Further layers would be optimising a 233 ms load — over-engineering
the very box we set out to protect.

## Guard rails

- Never add a synchronous vendor HTTP call (Octopus/Fox/Daikin/Quartz) inline in
  an `async def` handler — wrap in `asyncio.to_thread`. (`/scheduler/status` was
  fixed this way in #555; it still does a live Octopus fetch under the hood.)
- Caches that front historical data (lifetime, closed-month PnL) can be aggressive
  (hours). Caches fronting live state (SoC, prices) stay short and the live value
  must also be reachable on a fast always-fresh endpoint (`/cockpit/now`).
- Measure before/after with the same headless `network` capture; target for the
  above-the-fold wall is **< 1 s**.
