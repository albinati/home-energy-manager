/* v10.1 plan page — predicted vs actual overlay.
 *
 * Reads:
 *   - /api/v1/optimization/plan  → today's planned Fox groups + Daikin actions
 *   - /api/v1/schedule           → action_schedule status + execution timestamps
 *
 * V12 migration adds optimizer_logs.predicted_soc_path; once that lands the
 * Δ SoC column will render real numbers. Until then it shows "—" placeholders.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, toast } = window.HEM || {};

  function pad(n) { return String(n).padStart(2, '0'); }

  function buildSlots(now) {
    const slots = [];
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    for (let i = 0; i < 48; i++) {
      const s = new Date(start);
      s.setMinutes(s.getMinutes() + i * 30);
      const e = new Date(s);
      e.setMinutes(e.getMinutes() + 30);
      slots.push({ start: s, end: e, label: `${pad(s.getHours())}:${pad(s.getMinutes())}` });
    }
    return slots;
  }

  function findGroupForSlot(groups, slot) {
    return groups.find(g => {
      const gs = new Date(slot.start);
      gs.setHours(g.startHour, g.startMinute || 0, 0, 0);
      const ge = new Date(slot.start);
      ge.setHours(g.endHour, g.endMinute || 0, 0, 0);
      return slot.start >= gs && slot.start < ge;
    });
  }

  function workModeStrategy(g) {
    if (!g) return '—';
    const m = (g.workMode || '').toLowerCase();
    if (m.includes('forcecharge')) return `cheap (charge → ${g.extraParam?.fdSoc ?? '?'}%)`;
    if (m.includes('forcedischarge')) return 'peak (discharge)';
    if (m.includes('feed-in')) return 'export';
    if (m.includes('backup')) return 'backup';
    return 'standard';
  }

  function classifyDelta(plan, actual) {
    if (!actual) return 'is-pending';
    return 'is-positive';  // placeholder — needs real SoC comparison
  }

  async function load() {
    try {
      const [plan, sched] = await Promise.all([
        jsonFetch('/api/v1/optimization/plan'),
        jsonFetch('/api/v1/schedule'),
      ]);
      const fox = plan?.fox || {};
      let groups = [];
      try { groups = JSON.parse(fox.groups_json || '[]'); } catch (_e) {}
      const now = new Date();
      const slots = buildSlots(now);
      const tbody = $('#planTbody');
      tbody.innerHTML = '';
      slots.forEach(slot => {
        const g = findGroupForSlot(groups, slot);
        const isNow = now >= slot.start && now < slot.end;
        const isPast = slot.end <= now;
        const tr = document.createElement('tr');
        if (isNow) tr.classList.add('is-now');
        const plannedText = g ? `${g.workMode}` : '—';
        const strategyText = workModeStrategy(g);
        const actualText = isPast ? 'executed' : (isNow ? 'in progress' : '—');
        const deltaClass = classifyDelta(g, isPast ? 'executed' : null);
        tr.innerHTML = `
          <td>${slot.label}</td>
          <td>${plannedText}</td>
          <td>${strategyText}</td>
          <td class="col-actual ${deltaClass}">${actualText}</td>
          <td class="col-actual ${deltaClass}">—</td>`;
        tr.addEventListener('click', () => showSlotDetail(slot, g, sched));
        tbody.appendChild(tr);
      });
      if (slots.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-mute">No plan loaded.</td></tr>';
      }
    } catch (e) {
      toast(`Plan: ${e.message}`, 'bad');
    }
  }

  function showSlotDetail(slot, group, sched) {
    const card = $('#slotDetailCard');
    const daikin = (sched?.actions || []).filter(a => a.device === 'daikin' && a.start_time && new Date(a.start_time) >= slot.start && new Date(a.start_time) < slot.end);
    const detail = {
      slot: `${slot.label} → ${slot.end.toTimeString().slice(0,5)}`,
      planned_fox_group: group || null,
      daikin_actions_in_slot: daikin.length ? daikin : '(none — passive mode)',
      note: 'Δ SoC actual will populate once optimizer_logs.predicted_soc_path migration ships.',
    };
    $('#slotDetailBody').textContent = JSON.stringify(detail, null, 2);
    card.hidden = false;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('#btnReloadPlan')?.addEventListener('click', load);
    $('#btnCloseSlot')?.addEventListener('click', () => { $('#slotDetailCard').hidden = true; });
    load();
  });
})();
