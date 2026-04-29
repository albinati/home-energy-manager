/* v10.1 settings page — categorised, human-labelled, simulate-confirm.
 * Every change flows through wrapAction (simulate → modal → apply).
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, wrapAction, toast } = window.HEM || {};

  /* Per-key human metadata. Anything not in here falls back to the raw key.
   * v10.2: DAIKIN_CONTROL_MODE + REQUIRE_SIMULATION_ID moved to the topbar
   * mode badge / mode-switcher dialog (see _mode_switcher.html). They no
   * longer render here.
   */
  const META = {
    DHW_TEMP_NORMAL_C: {
      label: 'Normal hot-water target',
      desc: 'Target tank temperature on a typical day (°C). Higher = more buffer for evening showers but more standing loss.',
      group: 'settingsComfort',
    },
    DHW_TEMP_COMFORT_C: {
      label: 'Plunge ceiling for hot water',
      desc: 'Maximum tank temperature when the LP wants to absorb a cheap-price slot (°C). Only reached when it actively pays to do so.',
      group: 'settingsComfort',
    },
    INDOOR_SETPOINT_C: {
      label: 'Indoor target temperature',
      desc: 'Target room temperature used for the LP comfort constraint (°C).',
      group: 'settingsComfort',
    },
    OPTIMIZATION_PRESET: {
      label: 'Occupancy preset',
      desc:
        'normal = standard household. guests = higher hot water + warmer rooms. travel/away = frost protection only, max battery export. ' +
        '(BOOST retired in v10 — silently aliased to normal.)',
      group: 'settingsStrategy',
    },
    ENERGY_STRATEGY_MODE: {
      label: 'Energy strategy mode',
      desc: 'savings_first = LP allows discharging the battery to the grid during peak tariff (peak-export). strict_savings = never discharge to grid.',
      group: 'settingsStrategy',
    },
    LP_PLAN_PUSH_HOUR: {
      label: 'Nightly plan push hour (UTC)',
      desc: 'UTC hour when the next-day plan is force-pushed (anchored to Daikin quota rollover at 00:00 UTC).',
      group: 'settingsSchedule',
    },
    LP_PLAN_PUSH_MINUTE: {
      label: 'Nightly plan push minute',
      desc: 'UTC minute (paired with the hour above).',
      group: 'settingsSchedule',
    },
  };

  function fmtCurrent(item) {
    const v = item.value;
    if (v == null) return '—';
    if (Array.isArray(v)) return v.join(', ');
    if (typeof v === 'number') return v.toString();
    return String(v);
  }

  function controlFor(item) {
    if (item.enum) {
      return `<select data-key="${item.key}">
        ${item.enum.map(o => `<option value="${o}" ${String(item.value) === String(o) ? 'selected' : ''}>${o}</option>`).join('')}
      </select>`;
    }
    if (item.type === 'int' || item.type === 'float') {
      const step = item.type === 'int' ? '1' : '0.5';
      return `<input type="number" data-key="${item.key}" value="${item.value ?? ''}" step="${step}"
              ${item.min != null ? `min="${item.min}"` : ''}
              ${item.max != null ? `max="${item.max}"` : ''}
              style="width:5.5rem;">`;
    }
    return `<input type="text" data-key="${item.key}" value="${fmtCurrent(item)}" style="width:8rem;">`;
  }

  function badgeClass(item) {
    if (item.key === 'DAIKIN_CONTROL_MODE') {
      return item.value === 'passive' ? 'is-passive' : 'is-active';
    }
    if (item.key === 'REQUIRE_SIMULATION_ID') {
      return String(item.value) === 'true' ? 'is-active' : 'is-passive';
    }
    return '';
  }

  function renderInline(item, container) {
    const meta = META[item.key] || {};
    const block = document.createElement('div');
    block.className = 'setting-block' + (meta.danger ? ' is-danger' : '');
    block.innerHTML = `
      <div class="setting-head">
        <h3 class="setting-name">${meta.label || item.key}</h3>
        <span class="status-badge ${badgeClass(item)}">${fmtCurrent(item)}</span>
      </div>
      <p class="setting-desc">${meta.desc || item.description || ''}</p>
      <div class="setting-actions">
        ${controlFor(item)}
        <button class="btn btn-secondary btn-sm" data-apply="${item.key}">Change…</button>
      </div>`;
    bindControl(block, item);
    container.appendChild(block);
  }

  function bindControl(wrap, item) {
    const btn = wrap.querySelector(`[data-apply="${item.key}"]`);
    btn.addEventListener('click', async () => {
      const inp = wrap.querySelector(`[data-key="${item.key}"]`);
      let value = inp.value;
      if (item.type === 'int') value = parseInt(value);
      else if (item.type === 'float') value = parseFloat(value);
      const result = await wrapAction({
        method: 'PUT',
        simulateUrl: `/api/v1/settings/${item.key}/simulate`,
        applyUrl: `/api/v1/settings/${item.key}`,
        body: { value },
      });
      if (result.applied) load();
    });
  }

  /* ---------------------------------------------------------------------
   * Composite controls — hour + minute as two separate spinbox fields was
   * awful. Renders as a single <input type="time"> and applies both keys
   * in one batch so simulate → confirm fires only once.
   *
   * V12 removed the LP_MPC_HOURS 24-cell grid — the MPC is now fully
   * event-driven (tier_boundary + octopus_fetch + drift + forecast_revision
   * + plan_push), no fixed-hour cron to configure.
   * ---------------------------------------------------------------------
   */

  function renderPlanPushTime(hourItem, minuteItem, container) {
    const hh = String(hourItem.value ?? 0).padStart(2, '0');
    const mm = String(minuteItem.value ?? 0).padStart(2, '0');
    const block = document.createElement('div');
    block.className = 'setting-block';
    block.innerHTML = `
      <div class="setting-head">
        <h3 class="setting-name">Nightly plan push (UTC)</h3>
        <span class="status-badge">${hh}:${mm}</span>
      </div>
      <p class="setting-desc">When the next-day plan is pushed to Fox + Daikin. Anchored to UTC so it always lands just after the Daikin quota rollover at 00:00 UTC. Default 00:05 UTC.</p>
      <div class="setting-actions">
        <input type="time" id="planPushTime" value="${hh}:${mm}" step="60" style="width:8rem;">
        <button class="btn btn-secondary btn-sm" id="btnApplyPlanPushTime">Change…</button>
      </div>`;
    container.appendChild(block);
    block.querySelector('#btnApplyPlanPushTime').addEventListener('click', async () => {
      const raw = block.querySelector('#planPushTime').value || '00:05';
      const [h, m] = raw.split(':').map(n => parseInt(n, 10));
      if (isNaN(h) || isNaN(m)) { toast('Invalid time', 'warn'); return; }
      // Batch-apply both keys in one simulate → confirm round-trip so the
      // user sees a single diff, not two.
      const result = await wrapAction({
        simulateUrl: '/api/v1/settings/batch/simulate',
        applyUrl: '/api/v1/settings/batch',
        body: { changes: { LP_PLAN_PUSH_HOUR: h, LP_PLAN_PUSH_MINUTE: m } },
      });
      if (result.applied) load();
    });
  }

  async function load() {
    try {
      const resp = await jsonFetch('/api/v1/settings');
      const all = Array.isArray(resp) ? resp : (resp?.settings || []);
      const byKey = Object.fromEntries(all.map(s => [s.key, s]));

      // v10.2: DAIKIN_CONTROL_MODE + REQUIRE_SIMULATION_ID moved to the
      // topbar mode-switcher dialog. They no longer render here.

      // Grouped sections — schedule uses composite renderers for its two
      // awkward inputs (hour/minute pair → time picker; CSV hours → 24-cell grid).
      const simple = {
        settingsComfort: ['DHW_TEMP_NORMAL_C', 'DHW_TEMP_COMFORT_C', 'INDOOR_SETPOINT_C'],
        settingsStrategy: ['OPTIMIZATION_PRESET', 'ENERGY_STRATEGY_MODE'],
      };
      Object.entries(simple).forEach(([containerId, keys]) => {
        const c = $('#' + containerId);
        if (!c) return;
        c.innerHTML = '';
        keys.forEach(k => {
          const item = byKey[k];
          if (item) renderInline(item, c);
        });
      });

      const sched = $('#settingsSchedule');
      if (sched) {
        sched.innerHTML = '';
        if (byKey.LP_PLAN_PUSH_HOUR && byKey.LP_PLAN_PUSH_MINUTE) {
          renderPlanPushTime(byKey.LP_PLAN_PUSH_HOUR, byKey.LP_PLAN_PUSH_MINUTE, sched);
        }
      }
    } catch (e) {
      toast(`Settings: ${e.message}`, 'bad');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('#btnRollback')?.addEventListener('click', async () => {
      const result = await wrapAction({
        simulateUrl: '/api/v1/optimization/rollback/simulate',
        applyUrl: '/api/v1/optimization/rollback',
      });
      if (result.applied) load();
    });
    load();
  });
})();
