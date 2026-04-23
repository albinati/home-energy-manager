/* v10.2 mode switcher — opened from the topbar mode badge.
 *
 * Three independent toggles (OPERATION_MODE / DAIKIN_CONTROL_MODE /
 * REQUIRE_SIMULATION_ID), each routed through the standard wrapAction
 * (simulate → modal → confirm). When E5 (batch apply) ships, all three can be
 * batched into a single confirm; until then, each toggle is its own call.
 *
 * Reads its initial state from the badge's data-* attributes (server-rendered
 * on every page load) and keeps the badge + dialog in sync after any change.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const { wrapAction, jsonFetch } = window.HEM || {};

  const Switcher = {
    backdrop: null,
    closeBtn: null,
    dismissBtn: null,
    init() {
      if (this.backdrop) return;
      this.backdrop = $('#modeSwitcherBackdrop');
      if (!this.backdrop) return;  // partial not included on this page
      this.closeBtn = $('#modeSwitcherCloseBtn');
      this.dismissBtn = $('#modeSwitcherDismissBtn');

      this.closeBtn.addEventListener('click', () => this.close());
      this.dismissBtn.addEventListener('click', () => this.close());
      this.backdrop.addEventListener('click', e => {
        if (e.target === this.backdrop) this.close();
      });
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !this.backdrop.hidden) this.close();
      });

      // Operation mode buttons
      $$('[data-set-op-mode]').forEach(b => b.addEventListener('click', async () => {
        const mode = b.dataset.setOpMode;
        const result = await wrapAction({
          simulateUrl: '/api/v1/optimization/mode/simulate',
          applyUrl: '/api/v1/optimization/mode',
          body: { mode },
        });
        if (result.applied) await this.refresh();
      }));
      // Daikin control mode buttons
      $$('[data-set-daikin-mode]').forEach(b => b.addEventListener('click', async () => {
        const value = b.dataset.setDaikinMode;
        const result = await wrapAction({
          method: 'PUT',
          simulateUrl: '/api/v1/settings/DAIKIN_CONTROL_MODE/simulate',
          applyUrl: '/api/v1/settings/DAIKIN_CONTROL_MODE',
          body: { value },
        });
        if (result.applied) await this.refresh();
      }));
      // Require-simulation-id buttons
      $$('[data-set-require-sim]').forEach(b => b.addEventListener('click', async () => {
        const value = b.dataset.setRequireSim;
        const result = await wrapAction({
          method: 'PUT',
          simulateUrl: '/api/v1/settings/REQUIRE_SIMULATION_ID/simulate',
          applyUrl: '/api/v1/settings/REQUIRE_SIMULATION_ID',
          body: { value },
        });
        if (result.applied) await this.refresh();
      }));
    },

    async open() {
      this.init();
      if (!this.backdrop) return;
      this.backdrop.hidden = false;
      await this.refresh();
    },

    close() {
      if (this.backdrop) this.backdrop.hidden = true;
    },

    async refresh() {
      // Pull the live state from the API (more authoritative than the badge attrs)
      try {
        const status = await jsonFetch('/api/v1/optimization/status');
        const op = status?.operation_mode || status?.mode || '—';
        $('#msOpModeBadge').textContent = op;
        $('#msOpModeBadge').className = 'status-badge ' + (op === 'operational' ? 'is-active' : 'is-passive');
      } catch (_e) {}
      try {
        const r = await jsonFetch('/api/v1/settings/DAIKIN_CONTROL_MODE');
        const v = r?.value || '—';
        $('#msDaikinBadge').textContent = v;
        $('#msDaikinBadge').className = 'status-badge ' + (v === 'active' ? 'is-active' : 'is-passive');
      } catch (_e) {}
      try {
        const r = await jsonFetch('/api/v1/settings/REQUIRE_SIMULATION_ID');
        const v = String(r?.value).toLowerCase() === 'true';
        $('#msRequireSimBadge').textContent = v ? 'on' : 'off';
        $('#msRequireSimBadge').className = 'status-badge ' + (v ? 'is-active' : 'is-passive');
      } catch (_e) {}

      // Also update the topbar badge from these fresh values
      const badge = $('#modeBadge');
      if (badge) {
        // Trigger a soft reload of the page to re-render server-side rather than
        // duplicate badge-render logic. Cheaper than a SPA-style update; happens
        // only after a real change (the user just confirmed via modal).
        // Comment-out if you want zero reload — but then the badge can drift.
        // window.location.reload();
      }
    },
  };

  document.addEventListener('DOMContentLoaded', () => {
    const badge = $('#modeBadge');
    if (badge) badge.addEventListener('click', () => Switcher.open());
  });

  window.HEM = window.HEM || {};
  window.HEM.ModeSwitcher = Switcher;
})();
