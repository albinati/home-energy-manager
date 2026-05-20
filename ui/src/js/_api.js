// Shared API wrapper for the HEM SPA.
//
// Wraps fetch() to:
//   1. prepend the API base (/api/v1) so callers can write fetch('/cockpit/now')
//      and not care about the prefix.
//   2. inject `Authorization: Bearer ${window.__HEM_CONFIG__.bearer}` when the
//      bearer is present in the runtime config (set by ui-entrypoint.sh).
//   3. throw on non-2xx so callers can `.catch(e => ...)` instead of every
//      callsite re-checking response.ok.
//
// The shape is intentionally minimal — no axios, no class hierarchy. Callsites
// migrated from src/api/static/js/ can switch fetch('/api/v1/foo') →
// hemFetch('/foo') with no behaviour change.

(function (global) {
  "use strict";

  function _config() {
    return global.__HEM_CONFIG__ || { apiBase: "/api/v1", bearer: null };
  }

  function _join(base, path) {
    if (!path.startsWith("/")) path = "/" + path;
    return base.replace(/\/$/, "") + path;
  }

  function _withAuth(init) {
    const cfg = _config();
    const headers = new Headers((init && init.headers) || {});
    if (cfg.bearer && !headers.has("Authorization")) {
      headers.set("Authorization", "Bearer " + cfg.bearer);
    }
    return Object.assign({}, init || {}, { headers });
  }

  async function hemFetch(path, init) {
    const url = _join(_config().apiBase, path);
    const resp = await fetch(url, _withAuth(init));
    if (!resp.ok) {
      const err = new Error("hem-api " + resp.status + " " + resp.statusText);
      err.status = resp.status;
      try { err.body = await resp.text(); } catch (_) { err.body = ""; }
      throw err;
    }
    return resp;
  }

  // Convenience JSON helpers — the dominant shape across the existing JS files.
  async function hemGetJson(path, init) {
    const r = await hemFetch(path, init);
    return r.json();
  }

  async function hemPostJson(path, body, init) {
    const merged = Object.assign({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }, init || {});
    const r = await hemFetch(path, merged);
    return r.json();
  }

  global.hemFetch    = hemFetch;
  global.hemGetJson  = hemGetJson;
  global.hemPostJson = hemPostJson;

  // Transparent bearer injection on the global fetch.
  // The legacy JS files in src/api/static/js/ (lifted in Story B4) call
  // fetch('/api/v1/...') directly. Rather than rewrite every callsite,
  // we wrap the global fetch so any URL beginning with /api/ gets the
  // bearer header attached automatically. This is purely additive — if
  // the caller already set Authorization the header is left alone.
  const _originalFetch = global.fetch.bind(global);
  global.fetch = function patchedFetch(input, init) {
    let url;
    try {
      url = typeof input === "string" ? input : (input && input.url) || "";
    } catch (_) {
      url = "";
    }
    if (typeof url === "string" && url.startsWith("/api/")) {
      const cfg = _config();
      if (cfg.bearer) {
        const headers = new Headers((init && init.headers) || {});
        if (!headers.has("Authorization")) {
          headers.set("Authorization", "Bearer " + cfg.bearer);
        }
        init = Object.assign({}, init || {}, { headers });
      }
    }
    return _originalFetch(input, init);
  };
})(window);
