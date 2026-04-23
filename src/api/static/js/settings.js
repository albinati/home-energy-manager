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
    LP_MPC_HOURS: {
      label: 'Re-solve hours (local)',
      desc: 'Comma-separated local hours when the LP runs an intra-day re-solve (e.g. 6,12,21).',
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

  async function load() {
    try {
      const resp = await jsonFetch('/api/v1/settings');
      const all = Array.isArray(resp) ? resp : (resp?.settings || []);
      const byKey = Object.fromEntries(all.map(s => [s.key, s]));

      // v10.2: DAIKIN_CONTROL_MODE + REQUIRE_SIMULATION_ID + OPERATION_MODE
      // moved to the topbar mode-switcher dialog. They no longer render here.

      // Grouped sections
      const groups = {
        settingsComfort: ['DHW_TEMP_NORMAL_C', 'DHW_TEMP_COMFORT_C', 'INDOOR_SETPOINT_C'],
        settingsStrategy: ['OPTIMIZATION_PRESET', 'ENERGY_STRATEGY_MODE'],
        settingsSchedule: ['LP_PLAN_PUSH_HOUR', 'LP_PLAN_PUSH_MINUTE', 'LP_MPC_HOURS'],
      };
      Object.entries(groups).forEach(([containerId, keys]) => {
        const c = $('#' + containerId);
        if (!c) return;
        c.innerHTML = '';
        keys.forEach(k => {
          const item = byKey[k];
          if (item) renderInline(item, c);
        });
      });
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
