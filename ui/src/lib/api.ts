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
  const cfg = runtimeConfig();
  const headers = new Headers(init?.headers);
  if (cfg.bearer && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${cfg.bearer}`);
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

export async function getJson<T>(path: string, init?: FetchInit): Promise<T> {
  const r = await hemFetch(path, init);
  return r.json() as Promise<T>;
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
  return !!runtimeConfig().bearer;
}
