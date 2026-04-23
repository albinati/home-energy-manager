/* v10.1 settings page — categorised runtime knobs.
 * Every change flows through wrapAction (simulate → modal → apply).
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const { jsonFetch, wrapAction, toast } = window.HEM || {};

  const CATEGORIES = {
    settingsDaikin: ['DAIKIN_CONTROL_MODE', 'REQUIRE_SIMULATION_ID'],
    settingsStrategy: ['OPTIMIZATION_PRESET', 'ENERGY_STRATEGY_MODE'],
    settingsSchedule: ['LP_PLAN_PUSH_HOUR', 'LP_PLAN_PUSH_MINUTE', 'LP_MPC_HOURS'],
    settingsComfort: ['DHW_TEMP_COMFORT_C', 'DHW_TEMP_NORMAL_C', 'INDOOR_SETPOINT_C'],
  };

  function renderRow(item) {
    const wrap = document.createElement('div');
    wrap.className = 'settings-row';
    wrap.innerHTML = `
      <div>
        <div class="label">${item.key}</div>
        <div class="description">${item.description || ''}</div>
      </div>
      <div class="control">
        <span class="current">${formatValue(item.value)}</span>
        ${renderControl(item)}
      </div>`;
    bindControl(wrap, item);
    return wrap;
  }

  function formatValue(v) {
    if (v == null) return '—';
    if (Array.isArray(v)) return v.join(',');
    return String(v);
  }

  function renderControl(item) {
    if (item.enum) {
      return `<select data-key="${item.key}">
        ${item.enum.map(o => `<option value="${o}" ${String(item.value) === String(o) ? 'selected' : ''}>${o}</option>`).join('')}
      </select>
      <button class="btn btn-secondary btn-sm" data-apply="${item.key}">Apply</button>`;
    }
    if (item.type === 'int' || item.type === 'float') {
      const step = item.type === 'int' ? '1' : '0.5';
      return `<input type="number" data-key="${item.key}" value="${item.value ?? ''}" step="${step}"
              ${item.min != null ? `min="${item.min}"` : ''}
              ${item.max != null ? `max="${item.max}"` : ''}
              style="width:5.5rem;">
      <button class="btn btn-secondary btn-sm" data-apply="${item.key}">Apply</button>`;
    }
    return `<input type="text" data-key="${item.key}" value="${formatValue(item.value)}" style="width:8rem;">
            <button class="btn btn-secondary btn-sm" data-apply="${item.key}">Apply</button>`;
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
      // API returns {settings: [...]}; tolerate a bare list for older versions.
      const all = Array.isArray(resp) ? resp : (resp?.settings || []);
      const byKey = Object.fromEntries(all.map(s => [s.key, s]));
      Object.entries(CATEGORIES).forEach(([containerId, keys]) => {
        const c = $('#' + containerId);
        if (!c) return;
        c.innerHTML = '';
        keys.forEach(k => {
          const item = byKey[k];
          if (item) c.appendChild(renderRow(item));
        });
      });
      // Operation mode card
      const opStatus = await jsonFetch('/api/v1/optimization/status').catch(() => null);
      $('#currentOpMode').textContent = opStatus?.operation_mode || opStatus?.mode || '—';
    } catch (e) {
      toast(`Settings: ${e.message}`, 'bad');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-op-mode]').forEach(b => b.addEventListener('click', async () => {
      await wrapAction({
        simulateUrl: '/api/v1/optimization/mode/simulate',
        applyUrl: '/api/v1/optimization/mode',
        body: { mode: b.dataset.opMode },
      });
      load();
    }));
    $('#btnRollback')?.addEventListener('click', async () => {
      await wrapAction({
        simulateUrl: '/api/v1/optimization/rollback/simulate',
        applyUrl: '/api/v1/optimization/rollback',
      });
      load();
    });
    load();
  });
})();
