/* v10.1 insights page — comparisons + history. No hardware writes. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, toast } = window.HEM || {};

  function fmtP(v) { return v == null ? '—' : `${Number(v).toFixed(1)}p`; }
  function fmtPct(v) { return v == null ? '—' : `${Number(v).toFixed(0)}%`; }
  function fmtN(v) { return v == null ? '—' : Number(v).toFixed(2); }

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
      const d = await jsonFetch(`/api/v1/energy/period?${params}`).catch(() => null);
      if (!d || d.detail) {
        $('#insightPeriodLabel').textContent = d?.detail || `(no data for ${period})`;
        return;
      }
      $('#insightCost').textContent = fmtP(d.net_cost_pence);
      $('#insightSvt').textContent = fmtP(d.svt_delta_pence);
      $('#insightPvSelfUse').textContent = fmtPct(d.pv_self_use_pct);
      $('#insightCycles').textContent = fmtN(d.battery_cycles);
      $('#insightPeriodLabel').textContent = d.label || `(${period})`;
    } catch (e) {
      toast(`Insights: ${e.message}`, 'bad');
    }
  }

  async function runCompare() {
    try {
      const d = await jsonFetch('/api/v1/tariffs/compare', { method: 'POST', body: { months_back: 3 } });
      const rows = (d?.tariffs || []).slice(0, 10);
      if (!rows.length) {
        $('#tariffCompareTable').innerHTML = '<p class="text-dim">No comparison data returned.</p>';
        return;
      }
      const html = `<table class="plan-table">
        <thead><tr><th>Tariff</th><th>Est. monthly</th><th>vs current</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td>${r.tariff_code || r.name || '—'}</td>
          <td>${fmtP(r.estimated_monthly_pence)}</td>
          <td>${fmtP(r.delta_vs_current_pence)}</td>
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
