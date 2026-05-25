/* v10.2 mode switcher — opened from the topbar mode badge.
 *
 * The dialog stages pending toggles for DAIKIN_CONTROL_MODE /
 * REQUIRE_SIMULATION_ID; clicking "Review changes" sends them all through the
 * batch settings endpoint as ONE simulate → modal → confirm. Single-toggle
 * usage still works — the batch endpoint accepts a 1-key payload too.
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
    reviewBtn: null,
    pending: {},   // { KEY: stagedValue } — only diverges-from-current entries
    current: {},   // { KEY: livedValue from API }

    init() {
      if (this.backdrop) return;
      this.backdrop = $('#modeSwitcherBackdrop');
      if (!this.backdrop) return;
      this.closeBtn = $('#modeSwitcherCloseBtn');
      this.dismissBtn = $('#modeSwitcherDismissBtn');
      this.reviewBtn = $('#modeSwitcherReviewBtn');

      this.closeBtn.addEventListener('click', () => this.close());
      this.dismissBtn.addEventListener('click', () => this.close());
      this.backdrop.addEventListener('click', e => {
        if (e.target === this.backdrop) this.close();
      });
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !this.backdrop.hidden) this.close();
      });

      $$('[data-stage-key]').forEach(b => b.addEventListener('click', () => {
        this.stage(b.dataset.stageKey, b.dataset.stageValue);
      }));

      this.reviewBtn.addEventListener('click', () => this.submit());
    },

    async open() {
      this.init();
      if (!this.backdrop) return;
      this.pending = {};
      this.backdrop.hidden = false;
      await this.refresh();
      this.repaint();
    },

    close() {
      if (this.backdrop) this.backdrop.hidden = true;
      this.pending = {};
    },

    stage(key, rawValue) {
      const current = this.current[key];
      const value = this._coerceForCompare(key, rawValue);
      const cur = this._coerceForCompare(key, current);
      if (String(value) === String(cur)) {
        delete this.pending[key];
      } else {
        this.pending[key] = this._coerceForSubmit(key, rawValue);
      }
      this.repaint();
    },

    _coerceForCompare(key, v) {
      if (key === 'REQUIRE_SIMULATION_ID') return String(v).toLowerCase() === 'true';
      return v;
    },

    _coerceForSubmit(key, v) {
      if (key === 'REQUIRE_SIMULATION_ID') return String(v).toLowerCase() === 'true';
      return v;
    },

    repaint() {
      // Highlight active button (matches current OR pending state)
      $$('[data-pending-key]').forEach(group => {
        const key = group.dataset.pendingKey;
        const staged = key in this.pending ? this.pending[key] : null;
        const current = this.current[key];
        const effective = staged !== null ? staged : current;
        $$('[data-stage-key]', group).forEach(b => {
          const matches = String(this._coerceForCompare(key, b.dataset.stageValue))
                       === String(this._coerceForCompare(key, effective));
          b.classList.toggle('is-selected', matches);
          b.classList.toggle('is-pending', matches && staged !== null);
        });
      });
      const n = Object.keys(this.pending).length;
      this.reviewBtn.disabled = (n === 0);
      this.reviewBtn.textContent = n === 0 ? 'Review changes' : `Review ${n} change${n === 1 ? '' : 's'}`;
    },

    async submit() {
      const changes = { ...this.pending };
      if (Object.keys(changes).length === 0) return;
      const result = await wrapAction({
        simulateUrl: '/api/v1/settings/batch/simulate',
        applyUrl: '/api/v1/settings/batch',
        body: { changes },
      });
      if (result.applied) {
        this.pending = {};
        await this.refresh();
        this.repaint();
        // Soft reload so the topbar badge re-renders from the new server-side context.
        setTimeout(() => window.location.reload(), 400);
      }
    },

    async refresh() {
      try {
        const r = await jsonFetch('/api/v1/settings/DAIKIN_CONTROL_MODE');
        const v = r?.value || '—';
        this.current.DAIKIN_CONTROL_MODE = v;
        $('#msDaikinBadge').textContent = v;
        $('#msDaikinBadge').className = 'status-badge ' + (v === 'active' ? 'is-active' : 'is-passive');
      } catch (_e) {}
      try {
        const r = await jsonFetch('/api/v1/settings/REQUIRE_SIMULATION_ID');
        const v = String(r?.value).toLowerCase() === 'true';
        this.current.REQUIRE_SIMULATION_ID = v;
        $('#msRequireSimBadge').textContent = v ? 'on' : 'off';
        $('#msRequireSimBadge').className = 'status-badge ' + (v ? 'is-active' : 'is-passive');
      } catch (_e) {}
    },
  };

  document.addEventListener('DOMContentLoaded', () => {
    const badge = $('#modeBadge');
    if (badge) badge.addEventListener('click', () => Switcher.open());
  });

  window.HEM = window.HEM || {};
  window.HEM.ModeSwitcher = Switcher;
})();
