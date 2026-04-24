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

  // ---------------------------------------------------------------------
  // Merged 24-hour timeline (replaces the previous 3 separate strips).
  // One row of 48 cells: background = price kind, bottom accent bar =
  // Fox mode; vertical cursor overlays "now"; export shown as a thin
  // secondary row below for reference. Click any cell for details.
  // ---------------------------------------------------------------------

  let _lastAgile = null;
  let _lastGroups = [];

  async function loadTariff() {
    _lastAgile = await jsonFetch('/api/v1/agile/today').catch(() => null);
    renderMergedTimeline();
  }

  async function loadPlan() {
    const p = await jsonFetch('/api/v1/optimization/plan').catch(() => null);
    if (p) {
      const fox = p.fox || {};
      try { _lastGroups = JSON.parse(fox.groups_json || '[]'); } catch (_e) { _lastGroups = []; }
    }
    renderMergedTimeline();
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  function priceKind(p) {
    if (p == null) return 'standard';
    if (p < 0) return 'negative';
    if (_thresholds.cheap_p != null && p < _thresholds.cheap_p) return 'cheap';
    if (_thresholds.peak_p != null && p > _thresholds.peak_p) return 'peak';
    return 'standard';
  }

  function foxModeClass(workMode) {
    if (!workMode) return 'mode-none';
    const m = workMode.toLowerCase();
    if (m.includes('forcecharge')) return 'mode-force-charge';
    if (m.includes('forcedischarge')) return 'mode-force-discharge';
    if (m.includes('feed-in') || m.includes('feedin')) return 'mode-feed-in';
    if (m.includes('backup')) return 'mode-backup';
    return 'mode-self-use';
  }

  function findGroupForSlot(groups, slotStart) {
    // Fox groups are UTC-clock HH:MM windows. Anchor comparison on
    // slotStart's UTC day so midnight-crossing windows still match.
    const dayStart = new Date(slotStart);
    dayStart.setUTCHours(0, 0, 0, 0);
    return groups.find(g => {
      const gs = new Date(dayStart); gs.setUTCHours(g.startHour, g.startMinute || 0, 0, 0);
      let ge = new Date(dayStart); ge.setUTCHours(g.endHour, g.endMinute || 0, 0, 0);
      // Handle end=00:00 which Fox treats as end of day.
      if (ge <= gs) ge = new Date(ge.getTime() + 24 * 3600 * 1000);
      return slotStart >= gs && slotStart < ge;
    }) || null;
  }

  function renderMergedTimeline() {
    const strip = $('#timelineStrip');
    const exportStrip = $('#timelineExport');
    if (!strip) return;
    strip.innerHTML = '';
    if (exportStrip) exportStrip.innerHTML = '';

    const now = new Date();
    const dayStart = new Date(now); dayStart.setUTCHours(0, 0, 0, 0);
    const importSlots = _lastAgile?.import_slots || [];
    const exportSlots = _lastAgile?.export_slots || [];
    const importByIso = Object.fromEntries(importSlots.map(s => [s.valid_from, s]));
    const exportByIso = Object.fromEntries(exportSlots.map(s => [s.valid_from, s]));

    let nowIndex = -1;
    for (let i = 0; i < 48; i++) {
      const hour = Math.floor(i / 2);
      const min = (i % 2) * 30;
      const slotStart = new Date(dayStart); slotStart.setUTCHours(hour, min, 0, 0);
      const slotEnd = new Date(slotStart.getTime() + 30 * 60 * 1000);
      const iso = slotStart.toISOString().replace('.000Z', 'Z');
      const importP = importByIso[iso]?.p ?? null;
      const exportP = exportByIso[iso]?.p ?? null;
      const group = findGroupForSlot(_lastGroups, slotStart);
      const kind = priceKind(importP);
      const modeCls = foxModeClass(group?.workMode);

      const cell = document.createElement('button');
      cell.className = `tl-cell kind-${kind}`;
      cell.dataset.i = String(i);
      const lbl = (window.HEM && window.HEM.fmtSlotTime)
        ? window.HEM.fmtSlotTime(iso)
        : `${pad2(hour)}:${pad2(min)}`;
      cell.title = `${lbl} · ${kind}` + (importP != null ? ` · ${importP.toFixed(1)}p` : '')
                 + (group ? ` · ${group.workMode}` : '');
      // Accent bar at the bottom of the cell = Fox mode.
      cell.innerHTML = `<span class="tl-mode ${modeCls}"></span>`;
      if (now >= slotStart && now < slotEnd) {
        cell.classList.add('is-now');
        nowIndex = i;
      }
      cell.addEventListener('click', () => {
        openSlotDetail({
          label: lbl,
          isoStart: iso,
          isoEnd: slotEnd.toISOString(),
          kind, importP, exportP,
          workMode: group?.workMode || null,
          fdSoc: group?.extraParam?.fdSoc,
          fdPwr: group?.extraParam?.fdPwr,
          minSocOnGrid: group?.extraParam?.minSocOnGrid,
        });
      });
      strip.appendChild(cell);

      if (exportStrip) {
        const xc = document.createElement('div');
        xc.className = `tl-export-cell kind-${priceKind(exportP)}`;
        xc.title = `${lbl} export: ${exportP != null ? exportP.toFixed(1) + 'p' : '—'}`;
        exportStrip.appendChild(xc);
      }
    }

    // Position the vertical "now" cursor at the correct slot.
    const cursor = $('#timelineCursor');
    if (cursor && nowIndex >= 0) {
      cursor.style.display = 'block';
      cursor.style.left = `calc(${(nowIndex + 0.5) / 48 * 100}% - 1px)`;
    } else if (cursor) {
      cursor.style.display = 'none';
    }

    // Threshold markers — shown inline above the strip so the user reads
    // "this band is cheap because the threshold is here".
    const thrPeak = $('#thrPeak');
    const thrCheap = $('#thrCheap');
    if (thrPeak) thrPeak.textContent = _thresholds.peak_p != null ? `peak > ${_thresholds.peak_p.toFixed(1)}p` : 'peak —';
    if (thrCheap) thrCheap.textContent = _thresholds.cheap_p != null ? `cheap < ${_thresholds.cheap_p.toFixed(1)}p` : 'cheap —';

    // Summary line replaces the old "Current — / —" label.
    const summary = $('#timelineSummary');
    if (summary) {
      const curSlot = nowIndex >= 0 ? strip.children[nowIndex] : null;
      const curKind = curSlot ? curSlot.className.match(/kind-(\S+)/)?.[1] || '—' : '—';
      const curImport = _lastAgile?.current_import_p;
      const curExport = _lastAgile?.current_export_p;
      const curGroup = nowIndex >= 0 ? findGroupForSlot(_lastGroups,
        new Date(new Date(dayStart).setUTCHours(Math.floor(nowIndex / 2), (nowIndex % 2) * 30, 0, 0))) : null;
      summary.innerHTML = `Now: <strong>${curKind}</strong> · import ${fmtP(curImport)} · export ${fmtP(curExport)}` +
                        (curGroup ? ` · Fox <strong>${curGroup.workMode}</strong>` : '');
    }
  }

  function openSlotDetail(s) {
    const backdrop = $('#slotDetailBackdrop');
    const body = $('#slotDetailBody');
    const title = $('#slotDetailTitle');
    if (!backdrop || !body || !title) return;
    title.textContent = `${s.label} · ${s.kind}`;
    body.innerHTML = `
      <table class="slot-detail-table">
        <tr><th>Time (local)</th><td>${s.label} — ${(window.HEM && window.HEM.fmtSlotTime) ? window.HEM.fmtSlotTime(s.isoEnd) : s.isoEnd.slice(11, 16)}</td></tr>
        <tr><th>Kind</th><td class="kind-${s.kind}"><span class="legend-swatch" style="vertical-align:middle;margin-right:0.35rem;"></span>${s.kind}</td></tr>
        <tr><th>Import price</th><td>${s.importP != null ? s.importP.toFixed(2) + ' p/kWh' : '—'}</td></tr>
        <tr><th>Export price</th><td>${s.exportP != null ? s.exportP.toFixed(2) + ' p/kWh' : '—'}</td></tr>
        <tr><th>Fox mode</th><td>${s.workMode || '—'}</td></tr>
        <tr><th>fdSoc (target %)</th><td>${s.fdSoc != null ? s.fdSoc : '—'}</td></tr>
        <tr><th>fdPwr (W)</th><td>${s.fdPwr != null ? s.fdPwr : '—'}</td></tr>
        <tr><th>minSocOnGrid</th><td>${s.minSocOnGrid != null ? s.minSocOnGrid + '%' : '—'}</td></tr>
      </table>
    `;
    backdrop.hidden = false;
    document.body.classList.add('drawer-open');
  }

  function bindSlotDetail() {
    const backdrop = $('#slotDetailBackdrop');
    const closeBtn = $('#btnSlotDetailClose');
    if (!backdrop) return;
    const hide = () => { backdrop.hidden = true; document.body.classList.remove('drawer-open'); };
    closeBtn?.addEventListener('click', hide);
    backdrop.addEventListener('click', e => { if (e.target === backdrop) hide(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !backdrop.hidden) hide();
    });
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
    bindSlotDetail();
    // Load the hero first so thresholds are available when strips render.
    await loadNow();
    loadTariff();
    loadPlan();
    loadBreakdown();
  });
})();
