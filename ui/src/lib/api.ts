// Typed wrapper around fetch() that:
//   1. prepends window.__HEM_CONFIG__.apiBase (/api/v1) so callers write
//      hemFetch("/cockpit/now") and don't know about the prefix.
//   2. injects Authorization: Bearer <token> when present.
//   3. throws HemApiError on non-2xx so callers .catch instead of branching
//      on response.ok at every site.
//
// Mirrors src/lib/_api.js (the legacy vanilla wrapper) so behaviour is
// identical across the SPA and the legacy pages that still ship alongside.

type RuntimeConfig = {
  apiBase: string;
  bearer: string | null;
  buildSha?: string;
};

const DEFAULT_CONFIG: RuntimeConfig = {
  apiBase: "/api/v1",
  bearer: null,
};

function runtimeConfig(): RuntimeConfig {
  if (typeof window === "undefined") return DEFAULT_CONFIG;
  return window.__HEM_CONFIG__ || DEFAULT_CONFIG;
}

// ── Admin token (viewer/admin role model) ──────────────────────────────────
// The UI is a passive VIEWER by default: no Authorization header → the API
// serves read-only data. An admin "unlocks" by entering the admin secret,
// which we store here (+ localStorage) and send as the bearer on every request
// so the API grants write + Settings/Journal access. The baked config.js token
// is intentionally NOT used — viewer means no token. See lib/auth.ts.
const ADMIN_TOKEN_KEY = "hem.adminToken";
let _adminToken: string | null =
  typeof localStorage !== "undefined" ? localStorage.getItem(ADMIN_TOKEN_KEY) : null;

export function getAdminToken(): string | null {
  return _adminToken;
}

export function setAdminToken(token: string | null): void {
  _adminToken = token && token.trim() ? token.trim() : null;
  if (typeof localStorage === "undefined") return;
  if (_adminToken) localStorage.setItem(ADMIN_TOKEN_KEY, _adminToken);
  else localStorage.removeItem(ADMIN_TOKEN_KEY);
}

function joinUrl(base: string, path: string): string {
  const cleanPath = path.startsWith("/") ? path : "/" + path;
  const cleanBase = base.replace(/\/$/, "");
  return cleanBase + cleanPath;
}

export class HemApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, statusText: string, body: string) {
    super(`hem-api ${status} ${statusText}`);
    this.status = status;
    this.body = body;
  }
}

type FetchInit = RequestInit & { headers?: HeadersInit };

function withAuth(init: FetchInit | undefined): FetchInit {
  const headers = new Headers(init?.headers);
  // Admin token only — viewer sends no Authorization header. (The baked
  // config.js bearer is no longer used: it would be a token readable by any
  // viewer, so it must not carry privilege.)
  if (_adminToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${_adminToken}`);
  }
  return { ...(init || {}), headers };
}

export async function hemFetch(path: string, init?: FetchInit): Promise<Response> {
  const url = joinUrl(runtimeConfig().apiBase, path);
  const resp = await fetch(url, withAuth(init));
  if (!resp.ok) {
    let body = "";
    try {
      body = await resp.text();
    } catch {
      // ignore — body unavailable
    }
    throw new HemApiError(resp.status, resp.statusText, body);
  }
  return resp;
}

// In-flight GET coalescing. When several widgets request the same endpoint
// on the same paint (e.g. /daikin/quota from both the Heating widget and the
// footer chips), share ONE network request instead of firing duplicates.
// Keyed by path; the entry is cleared as soon as the promise settles, so this
// is a coalescer for concurrent calls, NOT a response cache — staleness is
// still governed by the polling hooks.
const _inflightGets = new Map<string, Promise<unknown>>();

export async function getJson<T>(path: string, init?: FetchInit): Promise<T> {
  // Only coalesce plain GETs with no custom init (the common widget case).
  // Anything with a custom body/headers bypasses the cache to stay correct.
  const coalescable = !init || (!init.body && !init.method);
  if (coalescable) {
    const existing = _inflightGets.get(path);
    if (existing) return existing as Promise<T>;
  }
  const p = hemFetch(path, init).then((r) => r.json() as Promise<T>);
  if (coalescable) {
    _inflightGets.set(path, p);
    p.finally(() => {
      // Clear only if this is still the tracked promise (avoid evicting a
      // newer in-flight request that replaced ours).
      if (_inflightGets.get(path) === p) _inflightGets.delete(path);
    });
  }
  return p;
}

export async function postJson<T>(
  path: string,
  body: unknown,
  init?: FetchInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const r = await hemFetch(path, {
    method: "POST",
    body: JSON.stringify(body ?? {}),
    ...(init || {}),
    headers,
  });
  return r.json() as Promise<T>;
}

export function buildSha(): string {
  return runtimeConfig().buildSha || (typeof __BUILD_SHA__ !== "undefined" ? __BUILD_SHA__ : "dev");
}

export function hasBearer(): boolean {
  return !!_adminToken;
}
