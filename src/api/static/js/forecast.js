/* Forecast page — renders /api/v1/optimization/inputs. Phase 3.
 *
 * Answers: "what will the LP see on its next run?". Pure read — opening
 * the page never triggers a cloud fetch.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const { jsonFetch, wrapAction, toast } = window.HEM || {};

  function fmtP(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(1)}p`;
  }
  function fmtC(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(1)}°C`;
  }
  function fmtKwh(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(3)} kWh`;
  }

  /**
   * Render a compact CSS-only sparkline: one vertical bar per slot, height
   * scaled within [0, max]. Keeps the tab render fast without pulling a
   * charting library.
   */
  function renderSpark(mount, values) {
    if (!mount) return;
    mount.innerHTML = '';
    const cleaned = values.map(v => (v == null || isNaN(v) ? null : Number(v)));
    const present = cleaned.filter(v => v != null);
    if (!present.length) {
      mount.textContent = '—';
      return;
    }
    const min = Math.min(0, ...present);
    const max = Math.max(...present);
    const span = max - min || 1;
    cleaned.forEach((v, i) => {
      const bar = document.createElement('span');
      bar.className = 'fc-bar' + (v == null ? ' is-missing' : '');
      if (v != null) {
        const h = Math.max(2, ((v - min) / span) * 100);
        bar.style.height = `${h}%`;
      }
      mount.appendChild(bar);
    });
  }

  function renderChips(mount, snapshot) {
    if (!mount) return;
    const entries = Object.entries(snapshot || {});
    if (!entries.length) { mount.textContent = '—'; return; }
    mount.innerHTML = '';
    entries.forEach(([k, v]) => {
      const chip = document.createElement('span');
      chip.className = 'fc-chip';
      const val = typeof v === 'number' ? (Number.isInteger(v) ? v : Number(v).toFixed(3)) : v;
      chip.innerHTML = `<strong>${k}</strong> <span>${val}</span>`;
      mount.appendChild(chip);
    });
  }

  function renderSlotTable(rows) {
    const tbody = $('#fcSlotTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-mute">No slot data. Run the optimizer once to populate inputs.</td></tr>';
      return;
    }
    rows.forEach(r => {
      const tr = document.createElement('tr');
      const t = (window.HEM && window.HEM.fmtSlotTime) ? window.HEM.fmtSlotTime(r.t_utc) : r.t_utc.slice(11, 16);
      tr.innerHTML = `
        <td>${t}</td>
        <td class="num">${fmtP(r.price_import_p)}</td>
        <td class="num">${fmtP(r.price_export_p)}</td>
        <td class="num">${fmtC(r.temp_c)}</td>
        <td class="num">${r.solar_w_m2 == null ? '—' : Number(r.solar_w_m2).toFixed(0)}</td>
        <td class="num">${fmtKwh(r.base_load_kwh)}</td>`;
      tbody.appendChild(tr);
    });
  }

  async function load() {
    let d;
    try {
      d = await jsonFetch('/api/v1/optimization/inputs');
    } catch (e) {
      toast(`Forecast: ${e.message}`, 'bad');
      return;
    }

    $('#fcHorizon').textContent = `${d.horizon_hours} h`;
    $('#fcTz').textContent = d.planner_tz || '—';
    $('#fcTomorrow').textContent = d.tomorrow_rates_available ? 'published' : 'not yet';
    $('#fcCheap').textContent = d.thresholds?.cheap_p != null ? fmtP(d.thresholds.cheap_p) : '—';
    $('#fcPeak').textContent = d.thresholds?.peak_p != null ? fmtP(d.thresholds.peak_p) : '—';
    $('#fcMicro').textContent = d.micro_climate_offset_c != null ? `${Number(d.micro_climate_offset_c).toFixed(2)} °C` : '—';

    const init = d.initial || {};
    $('#fcInitSoc').textContent = init.soc_kwh != null
      ? `${Number(init.soc_kwh).toFixed(2)} kWh (${init.soc_pct != null ? init.soc_pct.toFixed(0) + '%' : '—'})`
      : '—';
    $('#fcInitSocSrc').textContent = init.soc_source || '—';
    $('#fcInitTank').textContent = fmtC(init.tank_c);
    $('#fcInitTankSrc').textContent = init.tank_source || '—';
    $('#fcInitIndoor').textContent = fmtC(init.indoor_c);
    $('#fcInitIndoorSrc').textContent = init.indoor_source || '—';

    renderChips($('#fcConfigChips'), d.config_snapshot);

    const rows = d.slots || [];
    renderSpark($('#fcSparkPrice'), rows.map(r => r.price_import_p));
    renderSpark($('#fcSparkTemp'), rows.map(r => r.temp_c));
    renderSpark($('#fcSparkSolar'), rows.map(r => r.solar_w_m2));
    renderSpark($('#fcSparkLoad'), rows.map(r => r.base_load_kwh));
    renderSlotTable(rows);
  }

  function bind() {
    $('#btnForecastReload')?.addEventListener('click', load);
    $('#btnForecastReplan')?.addEventListener('click', async () => {
      // Use the existing simulate→confirm flow via wrapAction, then reload.
      const r = await wrapAction({
        simulateUrl: '/api/v1/optimization/propose/simulate',
        applyUrl: '/api/v1/optimization/propose',
      });
      if (r.applied) setTimeout(load, 4000);
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bind();
    load();
  });
})();
