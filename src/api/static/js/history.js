/* History page — Phase 4.
 *
 * Drives the date-time picker, calls /api/v1/cockpit/at?when=..., and
 * renders the same-shape hero + plan-vs-actual table from the LP snapshot
 * tables. Zero cloud calls.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, toast } = window.HEM || {};

  function fmtC(v) { return (v == null || isNaN(v)) ? '—' : `${Number(v).toFixed(1)}°C`; }
  function fmtP(v) { return (v == null || isNaN(v)) ? '—' : `${Number(v).toFixed(1)}p`; }
  function fmtKwh(v) { return (v == null || isNaN(v)) ? '—' : `${Number(v).toFixed(3)} kWh`; }
  function fmtPct(v) { return (v == null || isNaN(v)) ? '—' : `${Number(v).toFixed(0)}%`; }
  function sumList(v) {
    if (!Array.isArray(v)) return null;
    const nums = v.map(Number).filter(n => !Number.isNaN(n));
    return nums.length ? nums.reduce((a, b) => a + b, 0) : null;
  }

  function inputToIso(val) {
    // datetime-local gives "2026-04-24T10:00"; treat as UTC — the picker
    // displays the wall-clock moment the user typed, interpreted as UTC.
    if (!val) return null;
    return val.length <= 16 ? val + ':00Z' : val + 'Z';
  }

  async function replay(whenIso) {
    let data;
    try {
      data = await jsonFetch(`/api/v1/cockpit/at?when=${encodeURIComponent(whenIso)}`);
    } catch (e) {
      toast(`History: ${e.message}`, 'bad');
      return;
    }

    const cur = data.current_slot || {};
    const state = data.state || {};
    const plan = data.planned_slot || {};
    const li = data.lp_inputs || {};
    const lx = data.lp_exogenous || {};
    const src = data.source || {};

    // Source readout: which run + which log row fed this payload.
    const srcEl = $('#historySource');
    if (srcEl) {
      const parts = [];
      if (src.lp_run_at_utc) parts.push(`LP run ${src.lp_run_at_utc}`);
      if (src.run_id != null) parts.push(`run_id=${src.run_id}`);
      if (src.execution_log_timestamp) parts.push(`execution_log @ ${src.execution_log_timestamp}`);
      srcEl.textContent = parts.length ? `Source: ${parts.join(' · ')}` : 'No snapshots available for that moment.';
    }

    // State row.
    $('#hSoc').textContent = state.soc_pct != null ? `${fmtPct(state.soc_pct)} · ${fmtKwh(state.soc_kwh)}` : '—';
    $('#hLoad').textContent = state.load_kw != null ? `${Number(state.load_kw).toFixed(2)} kW` : '—';
    $('#hFoxMode').textContent = state.fox_mode || '—';
    $('#hTank').textContent = fmtC(state.tank_c);
    $('#hIndoor').textContent = fmtC(state.indoor_c);
    $('#hOutdoor').textContent = fmtC(state.outdoor_c);
    $('#hLwt').textContent = fmtC(state.lwt_c);
    $('#hKind').textContent = data.slot_kind || '—';
    $('#hPriceImp').textContent = fmtP(cur.price_import_p);
    $('#hPriceExp').textContent = fmtP(cur.price_export_p);

    // Plan vs actual (actuals come from state where telemetry exists).
    $('#paPlanImport').textContent = fmtKwh(plan.import_kwh);
    $('#paActImport').textContent = state.load_kw != null ? `~${Number(state.load_kw * 0.5).toFixed(3)} kWh` : '—';
    $('#paPlanChg').textContent = fmtKwh(plan.charge_kwh);
    $('#paPlanDis').textContent = fmtKwh(plan.discharge_kwh);
    $('#paPlanDhw').textContent = fmtKwh(plan.dhw_kwh);
    $('#paPlanSpace').textContent = fmtKwh(plan.space_kwh);
    $('#paPlanTank').textContent = fmtC(plan.tank_temp_c);
    $('#paActTank').textContent = fmtC(state.tank_c);
    $('#paPlanIndoor').textContent = fmtC(plan.indoor_temp_c);
    $('#paActIndoor').textContent = fmtC(state.indoor_c);
    $('#paPlanSoc').textContent = plan.soc_kwh != null ? `${Number(plan.soc_kwh).toFixed(2)} kWh` : '—';
    $('#paActSoc').textContent = state.soc_kwh != null ? `${Number(state.soc_kwh).toFixed(2)} kWh` : '—';

    // Attribution donut — fetched separately for the date of replay.
    try {
      const whenDate = whenIso.slice(0, 10);  // "2026-04-24"
      const a = await jsonFetch(`/api/v1/attribution/day?date=${whenDate}`);
      renderAttribution(a);
    } catch (_e) {
      renderAttribution(null);
    }

    // LP inputs at solve time.
    $('#liRunAt').textContent = li.run_at_utc || '—';
    $('#liPlanDate').textContent = li.plan_date || '—';
    $('#liHorizon').textContent = li.horizon_hours != null ? `${li.horizon_hours} h` : '—';
    $('#liSoc').textContent = li.soc_initial_kwh != null
      ? `${Number(li.soc_initial_kwh).toFixed(2)} kWh (source: ${li.soc_source || '?'})`
      : '—';
    $('#liTank').textContent = li.tank_initial_c != null
      ? `${fmtC(li.tank_initial_c)} (source: ${li.tank_source || '?'})`
      : '—';
    $('#liIndoor').textContent = li.indoor_initial_c != null
      ? `${fmtC(li.indoor_initial_c)} (source: ${li.indoor_source || '?'})`
      : '—';
    $('#liMicro').textContent = li.micro_climate_offset_c != null
      ? `${Number(li.micro_climate_offset_c).toFixed(2)} °C`
      : '—';
    $('#liCheap').textContent = fmtP(li.cheap_threshold_p);
    $('#liPeak').textContent = fmtP(li.peak_threshold_p);
    $('#liDkMode').textContent = li.daikin_control_mode || '—';
    $('#liPreset').textContent = li.optimization_preset || '—';

    const loadBits = lx.base_load_components || {};
    const weatherBits = lx.weather_adjustment || {};
    const tariffBits = lx.tariffs || {};
    const residualTotal = sumList(loadBits.residual_profile_kwh);
    const applianceTotal = sumList(loadBits.appliance_profile_kwh);
    $('#lxResidual').textContent = fmtKwh(residualTotal);
    $('#lxAppliance').textContent = fmtKwh(applianceTotal);
    $('#lxFlat').textContent = fmtKwh(loadBits.flat_fallback_kwh);
    $('#lxFoxMean').textContent = fmtKwh(loadBits.fox_mean_kwh_per_slot);
    $('#lxBuckets').textContent = loadBits.profile_bucket_count != null
      ? `${loadBits.profile_bucket_count} buckets`
      : '—';
    $('#lxFetch').textContent = weatherBits.forecast_fetch_at_utc || '—';
    $('#lxPvAdjust').textContent = weatherBits.today_factor != null
      ? `today=${Number(weatherBits.today_factor).toFixed(3)} flat=${Number(weatherBits.flat_scale ?? 1).toFixed(3)} cloud=${weatherBits.cloud_table_cells ?? 0} hourly=${weatherBits.hourly_table_cells ?? 0}`
      : '—';
    if (Array.isArray(tariffBits.export_price_pence) && tariffBits.export_price_pence.length) {
      const xs = tariffBits.export_price_pence.map(Number).filter(n => !Number.isNaN(n));
      const min = Math.min(...xs);
      const max = Math.max(...xs);
      $('#lxExport').textContent = `${xs.length} slots · ${fmtP(min)} to ${fmtP(max)}`;
    } else {
      $('#lxExport').textContent = tariffBits.uses_flat_export_rate ? 'flat export rate' : '—';
    }
  }

  /**
   * Render a CSS conic-gradient donut showing how solar was split across
   * self-use / battery / export for the replay date.
   */
  function renderAttribution(a) {
    const donut = $('#attributionDonut');
    const legend = $('#attributionLegend');
    if (!donut || !legend) return;
    if (!a || !a.available || !a.shares) {
      donut.style.background = 'var(--bg-card-2)';
      legend.textContent = a && !a.available
        ? `No rollup yet for ${a.date} (Fox rollup runs overnight).`
        : '—';
      return;
    }
    const s = a.shares;
    // conic-gradient sweep: self-use → battery → export.
    const sCol = '#4caf50', bCol = '#2196f3', eCol = '#ff9800';
    const end1 = s.self_use_pct;
    const end2 = end1 + s.battery_pct;
    donut.style.background =
      `conic-gradient(${sCol} 0 ${end1}%, ${bCol} ${end1}% ${end2}%, ${eCol} ${end2}% 100%)`;
    legend.innerHTML = `
      <div class="attr-item"><span class="attr-swatch" style="background:${sCol}"></span> Self-use ${s.self_use_pct}%</div>
      <div class="attr-item"><span class="attr-swatch" style="background:${bCol}"></span> Battery ${s.battery_pct}%</div>
      <div class="attr-item"><span class="attr-swatch" style="background:${eCol}"></span> Export ${s.export_pct}%</div>
      <div class="attr-totals">${Number(a.solar_kwh).toFixed(1)} kWh solar · ${Number(a.load_kwh).toFixed(1)} kWh load · ${Number(a.export_kwh).toFixed(1)} kWh export</div>
    `;
  }

  function bind() {
    const picker = $('#historyWhen');
    // Default to 1 hour ago, formatted for datetime-local input (no TZ).
    const d = new Date(Date.now() - 60 * 60 * 1000);
    const pad = n => String(n).padStart(2, '0');
    if (picker) {
      picker.value = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    }
    $('#btnHistoryJump')?.addEventListener('click', () => {
      const iso = inputToIso(picker?.value);
      if (iso) replay(iso);
    });
    $('#btnHistoryNow')?.addEventListener('click', async () => {
      const now = new Date();
      if (picker) {
        picker.value = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}T${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}`;
      }
      replay(now.toISOString());
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bind();
    const picker = $('#historyWhen');
    const iso = inputToIso(picker?.value);
    if (iso) replay(iso);
  });
})();
