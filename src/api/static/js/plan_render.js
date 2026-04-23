/* v10.2 — shared plan/slot table renderer.
 *
 * Used by the legacy Plan tab and the new Insights · Day view (and later by
 * the Workbench). Inputs are the JSON shapes already produced by:
 *   /api/v1/execution/today[?date=…]   → exec  (slots[], totals, data_quality_note)
 *   /api/v1/optimization/plan          → plan  (.fox.groups_json for strategy column)
 *   /api/v1/agile/day?date=…           → tariff (.slots with kind labels)
 *
 * Pure rendering — no fetching, no mutation outside the passed mount node.
 */
(function () {
  'use strict';

  function pad(n) { return String(n).padStart(2, '0'); }
  function fmtP(v) { return v == null ? '—' : `${Number(v).toFixed(1)}p`; }
  function fmtKwh(v) { return v == null ? '—' : `${Number(v).toFixed(2)}`; }

  function strategyForSlot(groups, slotUtc) {
    const t = new Date(slotUtc);
    const h = t.getHours();
    const m = t.getMinutes();
    const cur = (groups || []).find(g => {
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
    return ' kind-' + String(k).replace(/[^a-z0-9_]/g, '-').toLowerCase();
  }

  /**
   * Render the per-slot cost table.
   *
   * @param {object} opts
   * @param {HTMLTableElement} opts.table   — table element with thead already in DOM
   * @param {object} opts.exec              — /execution/today response shape
   * @param {object} [opts.plan]            — /optimization/plan response shape
   * @param {HTMLElement} [opts.dataQualityNoteEl]  — optional <p> to fill if note present
   * @param {HTMLElement} [opts.totalsEl]   — optional element for totals tiles
   */
  function renderSlotTable(opts) {
    const { table, exec, plan, dataQualityNoteEl, totalsEl } = opts;
    let groups = [];
    try { groups = JSON.parse(plan?.fox?.groups_json || '[]'); } catch (_e) {}

    const slots = exec?.slots || [];
    const t = exec?.totals || {};

    if (dataQualityNoteEl) {
      if (exec?.data_quality_note) {
        dataQualityNoteEl.textContent = '⚠ ' + exec.data_quality_note;
        dataQualityNoteEl.hidden = false;
      } else {
        dataQualityNoteEl.hidden = true;
      }
    }

    if (totalsEl) {
      const dlt = t.delta_vs_svt_p;
      const dCls = dlt == null ? '' : (dlt > 0 ? 'text-bad' : (dlt < 0 ? 'text-ok' : ''));
      totalsEl.querySelector('[data-tot=cost]')?.replaceChildren(document.createTextNode(fmtP(t.cost_realised_p)));
      const dEl = totalsEl.querySelector('[data-tot=delta]');
      if (dEl) {
        dEl.textContent = dlt == null ? '—' : `${dlt >= 0 ? '+' : ''}${dlt.toFixed(1)}p`;
        dEl.className = 'value ' + dCls;
      }
      const dShare = totalsEl.querySelector('[data-tot=daikin_share]');
      if (dShare) dShare.textContent = t.daikin_share_pct == null ? '—' : `${t.daikin_share_pct.toFixed(0)}%`;
      const dLoad = totalsEl.querySelector('[data-tot=load]');
      if (dLoad) dLoad.textContent = `${fmtKwh(t.load_kwh)} kWh`;
    }

    let tbody = table.querySelector('tbody');
    if (!tbody) { tbody = document.createElement('tbody'); table.appendChild(tbody); }
    let tfoot = table.querySelector('tfoot');
    if (!tfoot) { tfoot = document.createElement('tfoot'); table.appendChild(tfoot); }
    tfoot.innerHTML = '';

    if (!slots.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-mute">No execution rows yet for this date.</td></tr>';
      return;
    }

    tbody.innerHTML = '';
    let sumDaikin = 0, sumRes = 0, sumCost = 0, sumSvt = 0;
    slots.forEach(s => {
      sumDaikin += s.cost_daikin_p || 0;
      sumRes    += s.cost_residual_p || 0;
      sumCost   += s.cost_realised_p || 0;
      sumSvt    += s.cost_svt_p || 0;
      const d = new Date(s.slot_utc);
      const lbl = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      const strategy = strategyForSlot(groups, s.slot_utc) + (s.slot_kind ? ` · ${s.slot_kind}` : '');
      const dvs = s.delta_vs_svt_p || 0;
      const dvsCls = dvs > 0 ? 'text-bad' : (dvs < 0 ? 'text-ok' : '');
      const tr = document.createElement('tr');
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
      tbody.appendChild(tr);
    });

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
  }

  /**
   * Render the tariff strip (one cell per tariff slot, colour-coded by kind).
   *
   * @param {object} opts
   * @param {HTMLElement} opts.mount
   * @param {object} opts.tariff  — /api/v1/agile/day response
   * @param {string} [opts.tz]    — IANA tz for slot label rendering
   */
  function renderTariffStrip(opts) {
    const { mount, tariff } = opts;
    if (!mount) return;
    const slots = tariff?.slots || [];
    if (!slots.length) {
      mount.innerHTML = '<p class="text-mute" style="font-size:0.78rem;">No tariff data for this day.</p>';
      return;
    }
    const html = slots.map(s => {
      const t = new Date(s.valid_from);
      const lbl = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
      const tooltip = `${lbl} · ${Number(s.p).toFixed(2)}p · ${s.kind || ''}`;
      return `<div class="tariff-day-cell kind-${s.kind || 'standard'}" title="${tooltip}"><span class="tariff-day-cell-time">${lbl}</span><span class="tariff-day-cell-price">${Number(s.p).toFixed(0)}p</span></div>`;
    }).join('');
    mount.className = 'tariff-day-strip';
    mount.innerHTML = html;
  }

  window.HEM = window.HEM || {};
  window.HEM.PlanRender = { renderSlotTable, renderTariffStrip, fmtP, fmtKwh };
})();
