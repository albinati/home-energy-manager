/* v10.1 plan page — cost slot-by-slot, planned strategy vs realised cost.
 *
 * Reads:
 *   /api/v1/execution/today  → per-slot realised cost + Daikin attribution
 *   /api/v1/optimization/plan → today's planned Fox groups (for strategy column)
 *
 * Action: "Re-plan (simulate)" runs the simulate-then-confirm flow via the
 * shared wrapAction helper. No direct writes anywhere on this page.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, wrapAction, toast } = window.HEM || {};

  function pad(n) { return String(n).padStart(2, '0'); }
  function fmtP(v) { return v == null ? '—' : `${Number(v).toFixed(1)}p`; }
  function fmtKwh(v) { return v == null ? '—' : `${Number(v).toFixed(2)}`; }

  function strategyForSlot(groups, slotUtc) {
    const t = new Date(slotUtc);
    const h = t.getHours();
    const m = t.getMinutes();
    const cur = groups.find(g => {
      const inAfterStart = (h > g.startHour) || (h === g.startHour && m >= (g.startMinute || 0));
      const inBeforeEnd  = (h < g.endHour)   || (h === g.endHour   && m <  (g.endMinute   || 0));
      return inAfterStart && inBeforeEnd;
    });
    if (!cur) return '—';
    const wm = (cur.workMode || '').toLowerCase();
    if (wm.includes('forcecharge')) return `cheap → charge ${cur.extraParam?.fdSoc ?? '?'}%`;
    if (wm.includes('forcedischarge')) return 'peak → discharge';
    if (wm.includes('feed-in')) return 'export';
    if (wm.includes('backup')) return 'backup';
    return wm || '—';
  }

  function kindClass(k) {
    if (!k) return '';
    return ' kind-' + k.replace(/[^a-z0-9_]/g, '-').toLowerCase();
  }

  async function load() {
    try {
      const [exec, plan] = await Promise.all([
        jsonFetch('/api/v1/execution/today'),
        jsonFetch('/api/v1/optimization/plan').catch(() => ({})),
      ]);
      let groups = [];
      try { groups = JSON.parse(plan?.fox?.groups_json || '[]'); } catch (_e) {}

      const slots = exec?.slots || [];
      const t = exec?.totals || {};

      // Show the data-quality note + flag rows that look smoothed (all kWh equal)
      const note = $('#dataQualityNote');
      if (exec?.data_quality_note) {
        note.textContent = '⚠ ' + exec.data_quality_note;
        note.hidden = false;
      } else {
        note.hidden = true;
      }

      $('#totalCost').textContent = fmtP(t.cost_realised_p);
      const dlt = t.delta_vs_svt_p;
      const dEl = $('#totalDelta');
      dEl.textContent = dlt == null ? '—' : `${dlt >= 0 ? '+' : ''}${dlt.toFixed(1)}p`;
      dEl.className = 'value ' + (dlt > 0 ? 'text-bad' : (dlt < 0 ? 'text-ok' : ''));
      $('#totalDaikinShare').textContent = t.daikin_share_pct == null ? '—' : `${t.daikin_share_pct.toFixed(0)}%`;
      $('#totalLoad').textContent = `${fmtKwh(t.load_kwh)} kWh`;

      const tbody = $('#planTbody');
      const tfoot = $('#planTfoot');
      tfoot.innerHTML = '';
      if (!slots.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-mute">No execution rows yet today (heartbeat will populate as the day progresses).</td></tr>';
        return;
      }

      // Per-slot costs only — NOT cumulative. Each row sums Daikin+Residual = Cost.
      tbody.innerHTML = '';
      let sumDaikin = 0, sumRes = 0, sumCost = 0, sumSvt = 0;
      slots.forEach(s => {
        sumDaikin += s.cost_daikin_p || 0;
        sumRes    += s.cost_residual_p || 0;
        sumCost   += s.cost_realised_p || 0;
        sumSvt    += s.cost_svt_p || 0;
        const t = new Date(s.slot_utc);
        const lbl = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
        const strategy = strategyForSlot(groups, s.slot_utc) + (s.slot_kind ? ` · ${s.slot_kind}` : '');
        const tr = document.createElement('tr');
        const dvs = s.delta_vs_svt_p || 0;
        const dvsCls = dvs > 0 ? 'text-bad' : (dvs < 0 ? 'text-ok' : '');
        tr.className = kindClass(s.slot_kind);
        tr.innerHTML = `
          <td>${lbl}</td>
          <td>${strategy}</td>
          <td class="num">${fmtP(s.agile_p)}</td>
          <td class="num">${fmtKwh(s.consumption_kwh)}</td>
          <td class="num">${fmtP(s.cost_realised_p)}</td>
          <td class="num text-dim">${fmtP(s.cost_daikin_p)}</td>
          <td class="num text-dim">${fmtP(s.cost_residual_p)}</td>
          <td class="num text-mute">${fmtP(s.cost_svt_p)}</td>
          <td class="num ${dvsCls}">${dvs >= 0 ? '+' : ''}${dvs.toFixed(1)}p</td>`;
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', () => showDetail(s));
        tbody.appendChild(tr);
      });

      // Totals row in <tfoot>
      const totalDelta = sumCost - sumSvt;
      const totalDeltaCls = totalDelta > 0 ? 'text-bad' : (totalDelta < 0 ? 'text-ok' : '');
      tfoot.innerHTML = `<tr class="totals-row">
        <td><strong>Total</strong></td>
        <td class="text-dim">${slots.length} slots</td>
        <td class="num"></td>
        <td class="num"><strong>${fmtKwh(t.load_kwh)}</strong></td>
        <td class="num"><strong>${fmtP(sumCost)}</strong></td>
        <td class="num"><strong>${fmtP(sumDaikin)}</strong></td>
        <td class="num"><strong>${fmtP(sumRes)}</strong></td>
        <td class="num text-mute"><strong>${fmtP(sumSvt)}</strong></td>
        <td class="num ${totalDeltaCls}"><strong>${totalDelta >= 0 ? '+' : ''}${totalDelta.toFixed(1)}p</strong></td>
      </tr>`;
    } catch (e) {
      toast(`Plan: ${e.message}`, 'bad');
    }
  }

  function showDetail(slot) {
    const card = $('#slotDetailCard');
    $('#slotDetailBody').textContent = JSON.stringify(slot, null, 2);
    card.hidden = false;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('#btnReloadPlan')?.addEventListener('click', load);
    $('#btnCloseSlot')?.addEventListener('click', () => { $('#slotDetailCard').hidden = true; });
    $('#btnSimulateRePlan')?.addEventListener('click', async () => {
      const result = await wrapAction({
        simulateUrl: '/api/v1/optimization/propose/simulate',
        applyUrl:    '/api/v1/optimization/propose',
      });
      if (result.applied) {
        toast('Plan re-solve triggered', 'ok');
        setTimeout(load, 4000);
      }
    });
    load();
  });
})();
