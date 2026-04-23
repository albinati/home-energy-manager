/* Topbar quota pills — Daikin (180/day cap) + Fox (1200/day cap).
 * Reads /api/v1/{daikin,foxess}/quota. NEVER triggers a cloud refresh.
 * Single fetch on page load; no auto-refresh. Click pill to refresh manually.
 */
(function () {
  'use strict';

  async function readQuota(provider) {
    try {
      const r = await fetch(`/api/v1/${provider}/quota`, { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return null;
      return await r.json();
    } catch (_e) { return null; }
  }

  function renderPill(el, used, cap, prefix) {
    if (used == null || cap == null) {
      el.textContent = `${prefix} —/${cap || '?'}`;
      return;
    }
    el.textContent = `${prefix} ${used}/${cap}`;
    const ratio = used / cap;
    el.classList.toggle('is-warn', ratio >= 0.6 && ratio < 0.85);
    el.classList.toggle('is-bad', ratio >= 0.85);
  }

  async function refresh() {
    const dEl = document.getElementById('quotaDaikin');
    const fEl = document.getElementById('quotaFox');
    if (!dEl || !fEl) return;
    const [dq, fq] = await Promise.all([readQuota('daikin'), readQuota('foxess')]);
    if (dq) {
      const used = dq.calls_today ?? dq.used ?? dq.daily_used ?? 0;
      const cap = dq.daily_budget ?? dq.daily_limit ?? 180;
      renderPill(dEl, used, cap, 'D');
    }
    if (fq) {
      const used = fq.calls_today ?? fq.used ?? fq.daily_used ?? 0;
      const cap = fq.daily_budget ?? fq.daily_limit ?? 1200;
      renderPill(fEl, used, cap, 'F');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    refresh();
    document.getElementById('quotaPills')?.addEventListener('click', refresh);
  });

  window.HEM = window.HEM || {};
  window.HEM.refreshQuota = refresh;
})();
