/* v10.1+v10.2 plan page — slot-by-slot today.
 *
 * Per-slot rendering is delegated to PlanRender (plan_render.js) so the
 * Insights · Day view (v10.2) reuses the exact same shape.
 *
 * Action: "Re-plan (simulate)" runs the simulate-then-confirm flow via the
 * shared wrapAction helper. No direct writes anywhere on this page.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, wrapAction, toast, PlanRender } = window.HEM || {};

  async function load() {
    try {
      const [exec, plan] = await Promise.all([
        jsonFetch('/api/v1/execution/today'),
        jsonFetch('/api/v1/optimization/plan').catch(() => ({})),
      ]);
      PlanRender.renderSlotTable({
        table: $('#planTable'),
        exec,
        plan,
        dataQualityNoteEl: $('#dataQualityNote'),
        totalsEl: $('#planTotals'),
      });
      const slots = exec?.slots || [];
      const tbody = $('#planTbody');
      if (tbody) {
        // Wire click-to-detail (PlanRender doesn't bind handlers — keeps it pure).
        tbody.querySelectorAll('tr').forEach((tr, i) => {
          if (!slots[i]) return;
          tr.style.cursor = 'pointer';
          tr.addEventListener('click', () => showDetail(slots[i]));
        });
      }
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
