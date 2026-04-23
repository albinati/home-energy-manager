/* v10.1 cockpit — simulate → modal → confirm flow.
 *
 * Every action button on the cockpit/insights/plan/settings pages calls
 * wrapAction(simulateUrl, applyUrl, payload) instead of writing directly.
 * The /simulate endpoint never hits cloud APIs — it returns an ActionDiff
 * computed from cached state. The modal renders the diff, the operator
 * confirms, and the real-write happens with the X-Simulation-Id header.
 *
 * No frameworks; vanilla JS. ES2017+.
 */
(function () {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  function fmtJson(obj) {
    if (obj == null) return '—';
    try {
      return JSON.stringify(obj, null, 2);
    } catch (_e) {
      return String(obj);
    }
  }

  function classifyFlag(flag) {
    // Flags that require typed-confirmation are the most dangerous ones.
    const HARD = new Set([
      'enables_real_hardware_writes',
      'enables_daikin_writes',
      'plans_will_apply_without_review',
      'overwrites_fox_schedule',
      'may_lose_in_flight_dispatch',
    ]);
    return HARD.has(flag) ? 'hard' : 'soft';
  }

  const Modal = {
    backdrop: null,
    titleEl: null,
    summaryEl: null,
    beforeEl: null,
    afterEl: null,
    flagsSection: null,
    flagsList: null,
    flagsConfirmLabel: null,
    flagsConfirmInput: null,
    flagsConfirmWord: null,
    cancelBtn: null,
    applyBtn: null,
    closeBtn: null,
    impactSection: null,
    impactBody: null,
    _resolve: null,

    init() {
      if (this.backdrop) return;
      this.backdrop = $('#modalBackdrop');
      this.titleEl = $('#modalTitle');
      this.summaryEl = $('#modalSummary');
      this.beforeEl = $('#modalBefore');
      this.afterEl = $('#modalAfter');
      this.flagsSection = $('#modalFlagsSection');
      this.flagsList = $('#modalFlags');
      this.flagsConfirmLabel = $('#modalFlagsConfirmLabel');
      this.flagsConfirmInput = $('#modalFlagsConfirmInput');
      this.flagsConfirmWord = $('#modalFlagsConfirmWord');
      this.impactSection = $('#modalImpact');
      this.impactBody = $('#modalImpactBody');
      this.cancelBtn = $('#modalCancelBtn');
      this.applyBtn = $('#modalApplyBtn');
      this.closeBtn = $('#modalCloseBtn');

      const close = () => this._close(false);
      this.cancelBtn.addEventListener('click', close);
      this.closeBtn.addEventListener('click', close);
      this.backdrop.addEventListener('click', e => {
        if (e.target === this.backdrop) close();
      });
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !this.backdrop.hidden) close();
      });
      this.applyBtn.addEventListener('click', () => this._close(true));
      this.flagsConfirmInput.addEventListener('input', () => this._refreshApplyState());
    },

    show(diff) {
      this.init();
      this._diff = diff;
      this.titleEl.textContent = diff.action || 'Confirm';
      this.summaryEl.textContent = diff.human_summary || '';
      this.beforeEl.textContent = fmtJson(diff.before);
      this.afterEl.textContent = fmtJson(diff.after);

      const cost = diff.cost_delta_pence;
      const slots = diff.affected_slots || [];
      if (cost != null || slots.length) {
        const parts = [];
        if (cost != null) {
          const sign = cost >= 0 ? '+' : '';
          parts.push(`Estimated cost change: <strong>${sign}${cost.toFixed(1)}p</strong>`);
        }
        if (slots.length) {
          parts.push(`Affects ${slots.length} slot${slots.length === 1 ? '' : 's'}`);
        }
        this.impactBody.innerHTML = parts.join(' · ');
        this.impactSection.hidden = false;
      } else {
        this.impactSection.hidden = true;
      }

      const flags = diff.safety_flags || [];
      if (flags.length) {
        this.flagsList.innerHTML = flags.map(f => `<li><code>${escapeHtml(f)}</code></li>`).join('');
        this.flagsSection.hidden = false;
        const hardFlag = flags.find(f => classifyFlag(f) === 'hard');
        if (hardFlag) {
          this.flagsConfirmWord.textContent = hardFlag;
          this.flagsConfirmInput.value = '';
          this.flagsConfirmLabel.hidden = false;
          this._needTypedConfirm = hardFlag;
        } else {
          this.flagsConfirmLabel.hidden = true;
          this._needTypedConfirm = null;
        }
      } else {
        this.flagsSection.hidden = true;
        this.flagsConfirmLabel.hidden = true;
        this._needTypedConfirm = null;
      }

      this._refreshApplyState();
      this.backdrop.hidden = false;
      // Focus management
      setTimeout(() => {
        if (this._needTypedConfirm) this.flagsConfirmInput.focus();
        else this.applyBtn.focus();
      }, 30);

      return new Promise(resolve => { this._resolve = resolve; });
    },

    _refreshApplyState() {
      if (!this._needTypedConfirm) {
        this.applyBtn.disabled = false;
        return;
      }
      this.applyBtn.disabled = (this.flagsConfirmInput.value.trim() !== this._needTypedConfirm);
    },

    _close(confirmed) {
      this.backdrop.hidden = true;
      const r = this._resolve;
      this._resolve = null;
      if (r) r(confirmed);
    },
  };

  /* ----- HTTP helpers ----- */

  async function jsonFetch(url, opts = {}) {
    const init = Object.assign({
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    }, opts);
    if (init.body && typeof init.body === 'object') {
      init.body = JSON.stringify(init.body);
      init.headers['Content-Type'] = 'application/json';
    }
    const r = await fetch(url, init);
    let payload = null;
    try { payload = await r.json(); } catch (_e) { payload = null; }
    if (!r.ok) {
      const detail = (payload && payload.detail) || r.statusText;
      const msg = (typeof detail === 'object' && detail.message) ? detail.message : (typeof detail === 'string' ? detail : `HTTP ${r.status}`);
      const err = new Error(msg);
      err.status = r.status;
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  /* ----- Toast ----- */

  function toast(message, kind) {
    const stack = $('#toastStack');
    if (!stack) return;
    const t = document.createElement('div');
    t.className = 'toast' + (kind ? ` is-${kind}` : '');
    t.textContent = message;
    stack.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  /* ----- Public API ----- */

  /**
   * Run an action through simulate → modal → confirm → real apply.
   *
   * @param {object} opts
   * @param {string} opts.simulateUrl  — POST /api/v1/.../simulate or PUT /api/v1/settings/{key}/simulate
   * @param {string} opts.applyUrl     — paired real-write URL
   * @param {string} [opts.method=POST] — verb for both calls
   * @param {object} [opts.body]       — request body (same for both calls)
   * @returns {Promise<{applied: boolean, response?: any}>}
   */
  async function wrapAction(opts) {
    const method = opts.method || 'POST';
    const body = opts.body || {};
    let diff;
    try {
      diff = await jsonFetch(opts.simulateUrl, { method, body });
    } catch (e) {
      toast(`Simulate failed: ${e.message}`, 'bad');
      return { applied: false };
    }
    if (!diff || !diff.simulation_id) {
      toast('Simulate returned no diff — refusing to apply', 'bad');
      return { applied: false };
    }
    const confirmed = await Modal.show(diff);
    if (!confirmed) return { applied: false };

    try {
      const response = await jsonFetch(opts.applyUrl, {
        method,
        body,
        headers: { 'X-Simulation-Id': diff.simulation_id },
      });
      toast(`Applied: ${diff.action || 'change'}`, 'ok');
      return { applied: true, response };
    } catch (e) {
      const status = e.status || '?';
      toast(`Apply failed (${status}): ${e.message}`, 'bad');
      return { applied: false };
    }
  }

  // Expose as globals for inline event handlers in templates.
  window.HEM = window.HEM || {};
  window.HEM.wrapAction = wrapAction;
  window.HEM.toast = toast;
  window.HEM.jsonFetch = jsonFetch;
  window.HEM.Modal = Modal;
})();
