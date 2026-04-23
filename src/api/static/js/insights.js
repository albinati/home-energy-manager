/* v10.1 insights page — full economics breakdown + Daikin attribution. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, toast } = window.HEM || {};

  function fmtP(v) { return v == null ? '—' : `${Number(v).toFixed(1)}p`; }
  function fmtGBP(v) { return v == null ? '—' : `£${(Number(v) / 100).toFixed(2)}`; }
  function fmtKwh(v) { return v == null ? '—' : `${Number(v).toFixed(1)} kWh`; }
  function fmtPct(v) { return v == null ? '—' : `${Number(v).toFixed(1)}%`; }

  let currentPeriod = 'month';

  function periodToParams(period) {
    const now = new Date();
    const yyyy = now.getUTCFullYear();
    const mm = String(now.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(now.getUTCDate()).padStart(2, '0');
    if (period === 'month') return { period: 'month', month: `${yyyy}-${mm}` };
    if (period === 'year')  return { period: 'year',  year: String(yyyy) };
    if (period === 'week')  return { period: 'week',  week_start: `${yyyy}-${mm}-${dd}` };
    return { period: 'day', date: `${yyyy}-${mm}-${dd}` };
  }

  async function loadPeriod(period) {
    currentPeriod = period;
    document.querySelectorAll('.insights-period-btn').forEach(b => {
      b.classList.toggle('btn-primary', b.dataset.period === period);
      b.classList.toggle('btn-secondary', b.dataset.period !== period);
    });
    try {
      const params = new URLSearchParams(periodToParams(period));
      const d = await jsonFetch(`/api/v1/energy/period?${params}`).catch((e) => ({ _error: e.message }));
      if (!d || d.detail || d._error) {
        $('#insightPeriodLabel').textContent = d?.detail || d?._error || `(no data for ${period})`;
        return;
      }
      $('#insightPeriodLabel').textContent = d.period_label || `(${period})`;

      // Economics
      const c = d.cost || {};
      $('#ecoNet').textContent = fmtGBP(c.net_cost_pence);
      $('#ecoImport').textContent = fmtGBP(c.import_cost_pence);
      $('#ecoExport').textContent = fmtGBP(c.export_earnings_pence);
      $('#ecoStanding').textContent = fmtGBP(c.standing_charge_pence);

      const en = d.energy || {};
      $('#ecoImpKwh').textContent = fmtKwh(en.import_kwh);
      $('#ecoSolarKwh').textContent = fmtKwh(en.solar_kwh);
      $('#ecoLoadKwh').textContent = fmtKwh(en.load_kwh);
      $('#ecoExpKwh').textContent = fmtKwh(en.export_kwh);

      // Daikin
      $('#daikinKwh').textContent = fmtKwh(d.heating_estimate_kwh);
      $('#daikinCost').textContent = fmtGBP(d.heating_estimate_cost_pence);
      const ha = d.heating_analytics || {};
      $('#daikinPctCost').textContent = fmtPct(ha.heating_percent_of_cost);
      $('#daikinPctKwh').textContent = fmtPct(ha.heating_percent_of_consumption);

      // Gas comparison (if configured)
      if (d.equivalent_gas_cost_pence != null) {
        $('#gasCompareCard').hidden = false;
        $('#gasCost').textContent = fmtGBP(d.equivalent_gas_cost_pence);
        const adv = d.gas_comparison_ahead_pounds;
        $('#gasAdvantage').textContent = adv == null ? '—' : `£${Number(adv).toFixed(2)} saved`;
      } else {
        $('#gasCompareCard').hidden = true;
      }

      // Daily breakdown
      const tbody = $('#dailyTbody');
      const days = (d.chart_data || []).filter(r => (r.import_kwh || r.solar_kwh || r.load_kwh));
      if (!days.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-mute">No daily data for this period.</td></tr>';
      } else {
        tbody.innerHTML = days.map(r => `
          <tr>
            <td>${r.date}</td>
            <td class="num">${(r.import_kwh ?? 0).toFixed(1)}</td>
            <td class="num">${(r.export_kwh ?? 0).toFixed(1)}</td>
            <td class="num">${(r.solar_kwh ?? 0).toFixed(1)}</td>
            <td class="num">${(r.load_kwh ?? 0).toFixed(1)}</td>
            <td class="num text-dim">${(r.charge_kwh ?? 0).toFixed(1)}</td>
            <td class="num text-dim">${(r.discharge_kwh ?? 0).toFixed(1)}</td>
          </tr>`).join('');
      }
    } catch (e) {
      toast(`Insights: ${e.message}`, 'bad');
    }
  }

  async function runCompare() {
    try {
      const d = await jsonFetch('/api/v1/tariffs/compare', { method: 'POST', body: { months_back: 3 } });
      const rows = (d?.tariffs || d?.results || []).slice(0, 10);
      if (!rows.length) {
        $('#tariffCompareTable').innerHTML = '<p class="text-dim">No comparison data returned.</p>';
        return;
      }
      const html = `<table class="plan-table">
        <thead><tr><th>Tariff</th><th class="num">Est. monthly</th><th class="num">vs current</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td>${r.tariff_code || r.name || '—'}</td>
          <td class="num">${fmtGBP(r.estimated_monthly_pence)}</td>
          <td class="num">${fmtGBP(r.delta_vs_current_pence)}</td>
        </tr>`).join('')}</tbody></table>`;
      $('#tariffCompareTable').innerHTML = html;
    } catch (e) {
      $('#tariffCompareTable').innerHTML = `<p class="text-bad">${e.message}</p>`;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.insights-period-btn').forEach(b => {
      b.addEventListener('click', () => loadPeriod(b.dataset.period));
    });
    $('#btnRunCompare')?.addEventListener('click', runCompare);
    loadPeriod(currentPeriod);
  });
})();
