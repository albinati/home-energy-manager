/* v10.1 cockpit page logic.
 *
 * Reads from cached endpoints only — never triggers cloud refresh unless the
 * operator clicks the per-card ⟳ button (which routes through the same
 * endpoints; the backend decides whether to hit cloud APIs based on TTL +
 * quota).
 *
 * No auto-refresh. Operator triggers fresh data manually via per-card buttons.
 */
(function () {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const { jsonFetch, wrapAction, toast, refreshQuota } = window.HEM || {};

  /* ----- Freshness rendering ----- */

  function fmtAge(iso) {
    if (!iso) return { text: 'never', class: 'is-very-stale' };
    const d = new Date(iso);
    if (isNaN(d)) return { text: 'invalid', class: 'is-very-stale' };
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return { text: `${sec}s ago`, class: '' };
    if (sec < 3600) {
      const m = Math.floor(sec / 60);
      return { text: `${m}m ago`, class: m > 5 ? (m > 30 ? 'is-very-stale' : 'is-stale') : '' };
    }
    const h = Math.floor(sec / 3600);
    return { text: `${h}h ago`, class: 'is-very-stale' };
  }

  function setStaleness(card, iso) {
    const el = card.querySelector('[data-staleness]');
    const light = card.querySelector('.status-light');
    if (!el || !light) return;
    const a = fmtAge(iso);
    el.textContent = a.text;
    el.classList.remove('is-stale', 'is-very-stale');
    if (a.class) el.classList.add(a.class);
    light.classList.remove('is-ok', 'is-warn', 'is-bad');
    light.classList.add(a.class === 'is-very-stale' ? 'is-bad' : (a.class === 'is-stale' ? 'is-warn' : 'is-ok'));
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

  /* ----- Status cards ----- */

  async function loadFox(forceRefresh) {
    const card = $('#cardFox');
    try {
      const url = forceRefresh ? '/api/v1/foxess/status?refresh=true' : '/api/v1/foxess/status';
      const d = await jsonFetch(url);
      $('[data-fox-soc]', card).textContent = fmtPct(d.soc);
      $('[data-fox-solar]', card).textContent = fmtKwh(d.solar_power);
      $('[data-fox-load]', card).textContent = fmtKwh(d.load_power);
      $('[data-fox-grid]', card).textContent = fmtKwh(d.grid_power);
      $('[data-fox-mode]', card).textContent = d.work_mode || '—';
      setStaleness(card, d.updated_at);
    } catch (e) {
      $('[data-fox-soc]', card).textContent = 'err';
      toast(`Fox: ${e.message}`, 'bad');
    }
  }

  async function loadDaikin(forceRefresh) {
    const card = $('#cardDaikin');
    try {
      const url = forceRefresh ? '/api/v1/daikin/status?refresh=true' : '/api/v1/daikin/status';
      const arr = await jsonFetch(url);
      const d = (arr && arr[0]) || {};
      $('[data-daikin-tank]', card).textContent = fmtC(d.tank_temp);
      $('[data-daikin-indoor]', card).textContent = fmtC(d.room_temp);
      $('[data-daikin-outdoor]', card).textContent = fmtC(d.outdoor_temp);
      $('[data-daikin-lwt]', card).textContent = fmtC(d.lwt);
      const mode = d.control_mode || 'unknown';
      const badge = $('[data-daikin-mode]', card);
      badge.textContent = mode;
      badge.className = 'status-badge ' + (mode === 'passive' ? 'is-passive' : 'is-active');
      setStaleness(card, new Date().toISOString()); // Daikin status doesn't carry timestamp; stamp now
      // Echo mode into the override panel
      const tEl = $('[data-daikin-mode-text]');
      if (tEl) tEl.textContent = mode;
      $('#daikinOverridePassiveNote').hidden = (mode !== 'passive');
      $('#daikinOverrideActive').hidden = (mode === 'passive');
    } catch (e) {
      toast(`Daikin: ${e.message}`, 'bad');
    }
  }

  async function loadOctopus() {
    const card = $('#cardOctopus');
    try {
      // Use existing summary endpoint(s); fall back gracefully
      const status = await jsonFetch('/api/v1/optimization/status').catch(() => null);
      const importP = status?.current_price_pence ?? status?.now_price_pence ?? null;
      $('[data-octopus-import]', card).textContent = fmtP(importP);
      $('[data-octopus-export]', card).textContent = fmtP(status?.current_export_pence ?? null);
      $('[data-octopus-slots]', card).textContent = status?.agile_slots_loaded ?? '—';
      setStaleness(card, status?.last_octopus_fetch_at || null);
    } catch (e) {
      toast(`Octopus: ${e.message}`, 'bad');
    }
  }

  async function loadPlan() {
    const card = $('#cardPlan');
    try {
      const p = await jsonFetch('/api/v1/optimization/plan');
      $('[data-plan-backend]', card).textContent = p.optimizer_backend || '—';
      // Find current slot strategy from fox groups
      const fox = p.fox || {};
      let groups = [];
      try { groups = JSON.parse(fox.groups_json || '[]'); } catch (_e) {}
      const now = new Date();
      const cur = groups.find(g => slotContains(g, now));
      const next = groups.find(g => slotStartUTC(g) > now);
      $('[data-plan-now]', card).textContent = cur ? `${pad2(cur.startHour)}:${pad2(cur.startMinute)}–${pad2(cur.endHour)}:${pad2(cur.endMinute)} ${cur.workMode}` : '—';
      $('[data-plan-next]', card).textContent = next ? `${pad2(next.startHour)}:${pad2(next.startMinute)} → ${next.workMode}` : '—';
      setStaleness(card, fox.uploaded_at);
      renderPlanStrip(groups);
      renderTariffStrips(p);
    } catch (e) {
      toast(`Plan: ${e.message}`, 'bad');
    }
  }

  function pad2(n) { return String(n).padStart(2, '0'); }
  function slotStartUTC(g) {
    const d = new Date();
    d.setUTCHours(g.startHour || 0, g.startMinute || 0, 0, 0);
    return d;
  }
  function slotContains(g, t) {
    const s = slotStartUTC(g);
    const e = new Date(s);
    e.setUTCHours(g.endHour || 0, g.endMinute || 0, 0, 0);
    return t >= s && t < e;
  }

  function renderPlanStrip(groups) {
    const strip = $('#planStrip');
    if (!strip) return;
    strip.innerHTML = '';
    const now = new Date();
    // Build 48 half-hour slots from local 00:00 today
    for (let i = 0; i < 48; i++) {
      const slot = document.createElement('div');
      slot.className = 'plan-slot';
      const hour = Math.floor(i / 2);
      const min = (i % 2) * 30;
      const slotStart = new Date(now);
      slotStart.setHours(hour, min, 0, 0);
      const slotEnd = new Date(slotStart);
      slotEnd.setMinutes(slotEnd.getMinutes() + 30);

      // Find the group that covers this slot (using local time)
      const g = groups.find(g => {
        const gs = new Date(now);
        gs.setHours(g.startHour, g.startMinute || 0, 0, 0);
        const ge = new Date(now);
        ge.setHours(g.endHour, g.endMinute || 0, 0, 0);
        return slotStart >= gs && slotStart < ge;
      });
      const kind = workModeToKind(g);
      slot.classList.add(`kind-${kind}`);
      if (now >= slotStart && now < slotEnd) slot.classList.add('is-now');
      slot.title = `${pad2(hour)}:${pad2(min)} · ${kind}` + (g ? ` (${g.workMode})` : '');
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

  function renderTariffStrips(plan) {
    // Without a dedicated tariff endpoint we render a flat empty band as a placeholder.
    // Once /api/v1/tariffs/today (import + export per-slot) lands, fill these in.
    const importStrip = $('#tariffImportStrip');
    const exportStrip = $('#tariffExportStrip');
    if (!importStrip || !exportStrip) return;
    importStrip.innerHTML = exportStrip.innerHTML = '';
    for (let i = 0; i < 48; i++) {
      const im = document.createElement('div');
      im.className = 'tariff-slot';
      im.style.background = 'var(--bg-card-2)';
      importStrip.appendChild(im);
      const ex = document.createElement('div');
      ex.className = 'tariff-slot';
      ex.style.background = 'var(--bg-card-2)';
      exportStrip.appendChild(ex);
    }
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

  /* ----- Override panel ----- */

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
      loadFox();
      loadPlan();
    }));

    $('#btnProposeNow')?.addEventListener('click', async () => {
      await wrapAction({
        simulateUrl: '/api/v1/optimization/propose/simulate',
        applyUrl: '/api/v1/optimization/propose',
      });
      loadPlan();
    });

    $('#btnDaikinTank')?.addEventListener('click', async () => {
      const t = parseFloat($('#daikinTankInput').value);
      if (isNaN(t)) { toast('Enter a temperature', 'warn'); return; }
      await wrapAction({
        simulateUrl: '/api/v1/daikin/tank-temperature/simulate',
        applyUrl: '/api/v1/daikin/tank-temperature',
        body: { temperature: t },
      });
      loadDaikin();
    });

    $('#btnDaikinLwt')?.addEventListener('click', async () => {
      const off = parseFloat($('#daikinLwtInput').value);
      if (isNaN(off)) { toast('Enter an offset', 'warn'); return; }
      await wrapAction({
        simulateUrl: '/api/v1/daikin/lwt-offset/simulate',
        applyUrl: '/api/v1/daikin/lwt-offset',
        body: { offset: off },
      });
      loadDaikin();
    });
  }

  /* ----- Per-card refresh buttons ----- */

  function bindRefreshButtons() {
    $$('[data-refresh]').forEach(btn => btn.addEventListener('click', async () => {
      const which = btn.dataset.refresh;
      btn.disabled = true;
      try {
        if (which === 'fox') await loadFox(true);
        else if (which === 'daikin') await loadDaikin(true);
        else if (which === 'octopus') await loadOctopus();
        else if (which === 'plan') await loadPlan();
        if (refreshQuota) refreshQuota();
      } finally {
        btn.disabled = false;
      }
    }));
  }

  /* ----- Boot ----- */

  document.addEventListener('DOMContentLoaded', () => {
    bindOverride();
    bindRefreshButtons();
    // Initial loads — all from cache, no cloud refresh
    loadFox(false);
    loadDaikin(false);
    loadOctopus();
    loadPlan();
    loadBreakdown();
  });
})();
