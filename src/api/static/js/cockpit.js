/* Cockpit page — Phase 2 rework.
 *
 * The old four-card layout (Octopus / Fox / Daikin / Plan) has been replaced
 * by a single hero panel backed by /api/v1/cockpit/now — one coherent
 * snapshot of where we are now, with a freshness ribbon per source. The
 * tariff + plan strips and the house-load breakdown are still rendered below
 * via their existing endpoints.
 */
(function () {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const { jsonFetch, wrapAction, toast, refreshQuota } = window.HEM || {};

  // --- formatting helpers -------------------------------------------------

  function fmtAge(iso) {
    if (!iso) return { text: '—', class: 'is-very-stale' };
    const d = new Date(iso);
    if (isNaN(d)) return { text: '—', class: 'is-very-stale' };
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return { text: `${sec}s ago`, class: '' };
    if (sec < 3600) {
      const m = Math.floor(sec / 60);
      return { text: `${m}m ago`, class: m > 5 ? (m > 30 ? 'is-very-stale' : 'is-stale') : '' };
    }
    const h = Math.floor(sec / 3600);
    return { text: `${h}h ago`, class: 'is-very-stale' };
  }

  function fmtKwh(v, suffix) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(2)} ${suffix || 'kW'}`;
  }
  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(0)}%`;
  }
  function fmtC(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(1)}°C`;
  }
  function fmtP(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(1)}p`;
  }

  function slotKindForPrice(p, thresholds) {
    if (p == null) return 'standard';
    if (p < 0) return 'negative';
    const cheap = thresholds?.cheap_p;
    const peak = thresholds?.peak_p;
    if (cheap != null && p < cheap) return 'cheap';
    if (peak != null && p > peak) return 'peak';
    return 'standard';
  }

  // --- Hero panel ---------------------------------------------------------

  let _thresholds = { cheap_p: null, peak_p: null };

  async function loadNow() {
    let n;
    try {
      n = await jsonFetch('/api/v1/cockpit/now');
    } catch (e) {
      toast(`Cockpit: ${e.message}`, 'bad');
      return;
    }

    _thresholds = n.thresholds || { cheap_p: null, peak_p: null };

    const cur = n.current_slot || {};
    const state = n.state || {};
    const fresh = n.freshness || {};
    const next = n.next_transition || {};

    const kind = slotKindForPrice(cur.price_import_p, _thresholds);
    const heroKind = $('#heroSlotKind');
    if (heroKind) {
      heroKind.textContent = kind;
      heroKind.className = `hero-slot-kind kind-${kind}`;
    }
    const heroTime = $('#heroSlotTime');
    if (heroTime) {
      heroTime.textContent = (window.HEM && window.HEM.fmtSlotRange && cur.t_utc && cur.t_end_utc)
        ? window.HEM.fmtSlotRange(cur.t_utc, cur.t_end_utc)
        : '—';
    }
    const heroPrice = $('#heroSlotPrice');
    if (heroPrice) {
      const ip = cur.price_import_p != null ? `${cur.price_import_p.toFixed(1)}p` : '—';
      const ep = cur.price_export_p != null ? `${cur.price_export_p.toFixed(1)}p` : '—';
      heroPrice.textContent = `imp ${ip} · exp ${ep}`;
    }

    $('#heroSoc').textContent = state.soc_pct != null ? `${fmtPct(state.soc_pct)} · ${fmtKwh(state.soc_kwh, 'kWh')}` : '—';
    $('#heroSolar').textContent = fmtKwh(state.solar_kw);
    $('#heroLoad').textContent = fmtKwh(state.load_kw);
    const grid = state.grid_kw;
    $('#heroGrid').textContent = grid == null ? '—' : `${grid >= 0 ? '+' : ''}${Number(grid).toFixed(2)} kW`;
    $('#heroTank').textContent = fmtC(state.tank_c);
    $('#heroIndoor').textContent = fmtC(state.indoor_c);
    $('#heroOutdoor').textContent = fmtC(state.outdoor_c);
    $('#heroLwt').textContent = fmtC(state.lwt_c);
    $('#heroFoxMode').textContent = state.fox_mode || cur.fox_mode || '—';
    $('#heroDaikinMode').textContent = state.daikin_mode || n.modes?.daikin_control_mode || '—';

    // Phase 6 gating: grey-out (not hide) the Daikin tank/LWT controls when
    // DAIKIN_CONTROL_MODE is passive. The user sees they exist and understands
    // *why* they're inert — flipping hidden made the controls silently vanish.
    const dkMode = n.modes?.daikin_control_mode || 'passive';
    const isPassive = dkMode === 'passive';
    const passiveNote = $('#daikinOverridePassiveNote');
    const daikinBlock = $('#daikinOverrideActive');
    const modeText = $('[data-daikin-mode-text]');
    if (modeText) modeText.textContent = dkMode;
    if (passiveNote) passiveNote.hidden = !isPassive;
    if (daikinBlock) {
      daikinBlock.classList.toggle('is-disabled', isPassive);
      daikinBlock.querySelectorAll('input, button').forEach(el => { el.disabled = isPassive; });
    }

    // Next transition — countdown + label.
    const countdown = $('#heroNextCountdown');
    const nextLabel = $('#heroNextLabel');
    if (next.t_utc) {
      const until = Math.max(0, Math.floor((new Date(next.t_utc).getTime() - Date.now()) / 1000));
      const m = Math.floor(until / 60);
      const h = Math.floor(m / 60);
      countdown.textContent = h > 0 ? `in ${h}h ${m % 60}m` : `in ${m}m`;
      const t = (window.HEM && window.HEM.fmtSlotTime) ? window.HEM.fmtSlotTime(next.t_utc) : '';
      nextLabel.textContent = `${t} → ${next.new_fox_mode || '—'}`;
    } else {
      countdown.textContent = '—';
      nextLabel.textContent = '—';
    }

    // Freshness ribbon — one chip per source.
    ['agile', 'fox', 'daikin', 'plan'].forEach(k => {
      const chip = $(`.fresh-chip[data-source="${k}"]`);
      if (!chip) return;
      const f = fresh[k] || {};
      const a = fmtAge(f.fetched_at_utc);
      const ageEl = chip.querySelector('[data-age]');
      if (ageEl) ageEl.textContent = a.text;
      chip.classList.remove('is-stale', 'is-very-stale');
      if (a.class) chip.classList.add(a.class);
    });
  }

  // --- Tariff + Plan strips (reused; overlay now-cursor + thresholds) -----

  function priceColor(p) {
    if (p == null) return 'var(--bg-card-2)';
    const kind = slotKindForPrice(p, _thresholds);
    if (kind === 'negative') return 'var(--neg-price)';
    if (kind === 'cheap') return 'var(--cheap)';
    if (kind === 'peak') return 'var(--peak)';
    return 'var(--standard)';
  }

  async function loadTariff() {
    const agile = await jsonFetch('/api/v1/agile/today').catch(() => null);
    renderTariffStripsFromAgile(agile);
    updateTariffLabel(agile);
  }

  function renderTariffStripsFromAgile(agile) {
    const im = $('#tariffImportStrip');
    const ex = $('#tariffExportStrip');
    if (!im || !ex) return;
    im.innerHTML = ex.innerHTML = '';
    if (!agile) return;
    const now = new Date();
    const renderRow = (rowEl, slots) => {
      slots.forEach(s => {
        const cell = document.createElement('div');
        cell.className = 'tariff-slot';
        cell.style.background = priceColor(s.p);
        const rangeLbl = (window.HEM && window.HEM.fmtSlotRange)
          ? window.HEM.fmtSlotRange(s.valid_from, s.valid_to)
          : `${s.valid_from.slice(11,16)}–${s.valid_to.slice(11,16)}`;
        cell.title = `${rangeLbl} ${s.p.toFixed(1)}p`;
        const start = new Date(s.valid_from);
        const end = new Date(s.valid_to);
        if (now >= start && now < end) cell.classList.add('is-current');
        rowEl.appendChild(cell);
      });
    };
    renderRow(im, agile.import_slots || []);
    renderRow(ex, agile.export_slots || []);
  }

  function updateTariffLabel(agile) {
    const lbl = $('#tariffCurrentLabel');
    if (!lbl) return;
    if (!agile) { lbl.textContent = '—'; return; }
    const i = agile.current_import_p;
    const e = agile.current_export_p;
    const cheap = _thresholds.cheap_p != null ? `cheap<${_thresholds.cheap_p.toFixed(1)}p` : '';
    const peak = _thresholds.peak_p != null ? `peak>${_thresholds.peak_p.toFixed(1)}p` : '';
    const thr = [cheap, peak].filter(Boolean).join(' · ');
    lbl.textContent = `Current: import ${fmtP(i)} · export ${fmtP(e)}${thr ? ' · ' + thr : ''}`;
  }

  async function loadPlan() {
    const p = await jsonFetch('/api/v1/optimization/plan').catch(() => null);
    if (!p) return;
    const fox = p.fox || {};
    let groups = [];
    try { groups = JSON.parse(fox.groups_json || '[]'); } catch (_e) {}
    renderPlanStrip(groups);
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  function renderPlanStrip(groups) {
    const strip = $('#planStrip');
    if (!strip) return;
    strip.innerHTML = '';
    const now = new Date();
    // Fox groups are UTC-clock (hardware); build 48 UTC slots for the next 24h.
    const dayStart = new Date(now);
    dayStart.setUTCHours(0, 0, 0, 0);
    for (let i = 0; i < 48; i++) {
      const slot = document.createElement('div');
      slot.className = 'plan-slot';
      const hour = Math.floor(i / 2);
      const min = (i % 2) * 30;
      const slotStart = new Date(dayStart);
      slotStart.setUTCHours(hour, min, 0, 0);
      const slotEnd = new Date(slotStart);
      slotEnd.setUTCMinutes(slotEnd.getUTCMinutes() + 30);

      const g = groups.find(g => {
        const gs = new Date(dayStart);
        gs.setUTCHours(g.startHour, g.startMinute || 0, 0, 0);
        const ge = new Date(dayStart);
        ge.setUTCHours(g.endHour, g.endMinute || 0, 0, 0);
        return slotStart >= gs && slotStart < ge;
      });
      const kind = workModeToKind(g);
      slot.classList.add(`kind-${kind}`);
      if (now >= slotStart && now < slotEnd) slot.classList.add('is-now');
      const lbl = (window.HEM && window.HEM.fmtSlotTime)
        ? window.HEM.fmtSlotTime(slotStart.toISOString())
        : `${pad2(hour)}:${pad2(min)}`;
      slot.title = `${lbl} · ${kind}` + (g ? ` (${g.workMode})` : '');
      slot.addEventListener('click', () => { window.location.href = '/plan'; });
      strip.appendChild(slot);
    }
  }

  function workModeToKind(g) {
    if (!g) return 'standard';
    const m = (g.workMode || '').toLowerCase();
    if (m.includes('forcecharge')) return 'cheap';
    if (m.includes('feed-in')) return 'peak_export';
    if (m.includes('forcedischarge')) return 'peak';
    if (m.includes('backup')) return 'solar_charge';
    return 'standard';
  }

  async function loadBreakdown() {
    try {
      const b = await jsonFetch('/api/v1/load/breakdown');
      $('[data-load-total]').textContent = fmtKwh(b.house_total_kw);
      $('[data-load-daikin]').textContent = fmtKwh(b.daikin_estimate_kw);
      $('[data-load-residual]').textContent = fmtKwh(b.residual_kw);
      $('[data-load-source]').textContent = b.daikin_source === 'physics_instantaneous'
        ? 'Daikin estimate from cached outdoor temp + climate curve. Daily-anchor calibration arrives with backfill.'
        : `Daikin source: ${b.daikin_source}`;
    } catch (e) {
      toast(`Breakdown: ${e.message}`, 'bad');
    }
  }

  // --- Freshness chip handlers -------------------------------------------

  function bindFreshnessChips() {
    $$('.fresh-chip').forEach(chip => chip.addEventListener('click', async () => {
      const source = chip.dataset.source;
      chip.disabled = true;
      try {
        if (source === 'agile') {
          await jsonFetch('/api/v1/optimization/refresh', { method: 'POST' });
        } else if (source === 'fox') {
          await jsonFetch('/api/v1/foxess/status?refresh=true');
        } else if (source === 'daikin') {
          await jsonFetch('/api/v1/daikin/status?refresh=true');
        } else if (source === 'plan') {
          // No side-effect endpoint — just a reload of the current plan view.
        }
        await loadNow();
        if (source === 'agile') await loadTariff();
        if (source === 'plan') await loadPlan();
        if (refreshQuota) refreshQuota();
      } catch (e) {
        toast(`${source} refresh failed: ${e.message}`, 'bad');
      } finally {
        chip.disabled = false;
      }
    }));
  }

  // --- Manual override panel (unchanged from v10.1) -----------------------

  function bindOverride() {
    const toggle = $('#overrideToggle');
    const body = $('#overrideBody');
    if (toggle) toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!open));
      body.hidden = open;
    });
    $$('.override-tab').forEach(t => t.addEventListener('click', () => {
      $$('.override-tab').forEach(x => x.classList.remove('is-active'));
      t.classList.add('is-active');
      const id = t.dataset.tabTarget;
      $$('.override-tab-panel').forEach(p => p.hidden = (p.id !== id));
    }));

    $$('[data-fox-mode]').forEach(b => b.addEventListener('click', async () => {
      const mode = b.dataset.foxMode;
      await wrapAction({
        simulateUrl: '/api/v1/foxess/mode/simulate',
        applyUrl: '/api/v1/foxess/mode',
        body: { mode },
      });
      loadNow();
      loadPlan();
    }));

    $('#btnProposeNow')?.addEventListener('click', async () => {
      await wrapAction({
        simulateUrl: '/api/v1/optimization/propose/simulate',
        applyUrl: '/api/v1/optimization/propose',
      });
      loadPlan();
      loadNow();
    });

    $('#btnSimulateRePlanCockpit')?.addEventListener('click', async () => {
      const result = await wrapAction({
        simulateUrl: '/api/v1/optimization/propose/simulate',
        applyUrl: '/api/v1/optimization/propose',
      });
      if (result.applied) setTimeout(() => { loadPlan(); loadNow(); }, 4000);
    });

    $('#btnDaikinTank')?.addEventListener('click', async () => {
      const t = parseFloat($('#daikinTankInput').value);
      if (isNaN(t)) { toast('Enter a temperature', 'warn'); return; }
      await wrapAction({
        simulateUrl: '/api/v1/daikin/tank-temperature/simulate',
        applyUrl: '/api/v1/daikin/tank-temperature',
        body: { temperature: t },
      });
      loadNow();
    });

    $('#btnDaikinLwt')?.addEventListener('click', async () => {
      const off = parseFloat($('#daikinLwtInput').value);
      if (isNaN(off)) { toast('Enter an offset', 'warn'); return; }
      await wrapAction({
        simulateUrl: '/api/v1/daikin/lwt-offset/simulate',
        applyUrl: '/api/v1/daikin/lwt-offset',
        body: { offset: off },
      });
      loadNow();
    });
  }

  // --- Boot ---------------------------------------------------------------

  // --- Settings drawer ---------------------------------------------------
  // The drawer reuses settings.js — the container IDs on the cockpit page
  // (#settingsComfort / #settingsStrategy / #settingsSchedule) match the
  // full /settings page, so settings.js's own load() populates both.
  function bindSettingsDrawer() {
    const open = $('#btnSettingsDrawer');
    const backdrop = $('#settingsDrawerBackdrop');
    const closeBtn = $('#btnSettingsDrawerClose');
    if (!backdrop) return;
    const show = () => { backdrop.hidden = false; document.body.classList.add('drawer-open'); };
    const hide = () => { backdrop.hidden = true; document.body.classList.remove('drawer-open'); };
    open?.addEventListener('click', show);
    closeBtn?.addEventListener('click', hide);
    backdrop.addEventListener('click', e => { if (e.target === backdrop) hide(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !backdrop.hidden) hide();
    });
  }

  document.addEventListener('DOMContentLoaded', async () => {
    bindOverride();
    bindFreshnessChips();
    bindSettingsDrawer();
    // Load the hero first so thresholds are available when strips render.
    await loadNow();
    loadTariff();
    loadPlan();
    loadBreakdown();
  });
})();
