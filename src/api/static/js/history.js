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
