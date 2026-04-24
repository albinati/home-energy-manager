/* v10.2 E3 Workbench — tune, simulate, compare, promote.
 *
 * State machine:
 *   1. On load: GET /api/v1/workbench/schema → render grouped editor
 *   2. User edits → values stored locally; "Run simulation" POSTs current
 *      overrides to /api/v1/workbench/simulate → renders Compare table
 *   3. "Promote to prod" → wrapAction({simulateUrl, applyUrl}) using
 *      /api/v1/workbench/promote/* → batch-confirm via the standard modal.
 *   4. Profiles: GET/POST/DELETE /api/v1/workbench/profiles*.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const { jsonFetch, wrapAction, toast } = window.HEM || {};

  const State = {
    schema: null,         // GET /workbench/schema response
    overrides: {},        // KEY -> value (only diverges-from-current entries)
    lastSim: null,        // GET /workbench/simulate response
    currentPlan: null,    // GET /optimization/plan response
  };

  function fmt(v) {
    if (v == null) return '—';
    if (typeof v === 'number') return Math.abs(v) < 0.01 ? v.toString() : (Math.round(v * 1000) / 1000).toString();
    return String(v);
  }

  function fieldId(key) { return `wbf-${key}`; }

  function renderEditor() {
    const groups = State.schema?.groups || [];
    const fields = State.schema?.fields || [];
    const byGroup = {};
    for (const f of fields) {
      (byGroup[f.group] = byGroup[f.group] || []).push(f);
    }
    const groupTitles = {
      comfort: 'Comfort', battery: 'Battery', hardware: 'Hardware',
      penalty: 'Penalties', solver: 'Solver', schedule: 'Schedule', mode: 'Mode',
    };
    const html = groups.filter(g => byGroup[g]).map(g => `
      <details class="editor-group" open>
        <summary class="editor-group-title">${groupTitles[g] || g}</summary>
        <div class="editor-fields">
          ${byGroup[g].map(f => renderField(f)).join('')}
        </div>
      </details>
    `).join('');
    $('#editorBody').innerHTML = html;

    // Wire input events
    $$('input[data-key], select[data-key]').forEach(input => {
      input.addEventListener('input', () => {
        const key = input.dataset.key;
        const f = fields.find(x => x.key === key);
        const raw = input.value;
        if (raw === '' || raw == null) {
          delete State.overrides[key];
        } else {
          let v;
          if (f.type === 'float') v = parseFloat(raw);
          else if (f.type === 'int') v = parseInt(raw, 10);
          else v = String(raw);
          if (Number.isNaN(v)) { delete State.overrides[key]; }
          else if (v === f.current) { delete State.overrides[key]; }
          else State.overrides[key] = v;
        }
        repaintHeader();
        input.classList.toggle('is-dirty', key in State.overrides);
      });
    });
    repaintHeader();
  }

  function renderField(f) {
    const promoBadge = f.promotable ? '<span class="badge-promotable" title="Promotable to prod">●</span>' : '';
    const meta = [];
    if (f.min != null) meta.push(`min ${f.min}`);
    if (f.max != null) meta.push(`max ${f.max}`);
    if (f.enum) meta.push(`one of [${f.enum.join('|')}]`);
    const metaLine = meta.length ? `<span class="editor-field-meta">${meta.join(' · ')}</span>` : '';
    let inputHtml;
    if (f.enum) {
      inputHtml = `<select id="${fieldId(f.key)}" data-key="${f.key}" class="editor-input">
        ${f.enum.map(opt => `<option value="${opt}" ${String(opt) === String(f.current) ? 'selected' : ''}>${opt}</option>`).join('')}
      </select>`;
    } else {
      const t = f.type === 'int' ? 'number' : (f.type === 'float' ? 'number' : 'text');
      const step = f.type === 'int' ? '1' : 'any';
      inputHtml = `<input type="${t}" step="${step}" id="${fieldId(f.key)}" data-key="${f.key}" class="editor-input"
        value="${f.current == null ? '' : f.current}" placeholder="${fmt(f.current)}">`;
    }
    return `<div class="editor-field">
        <label class="editor-field-label" for="${fieldId(f.key)}">${promoBadge} ${f.key}</label>
        <p class="editor-field-desc">${f.description || ''} ${metaLine}</p>
        ${inputHtml}
      </div>`;
  }

  function repaintHeader() {
    const n = Object.keys(State.overrides).length;
    $('#btnPromote').disabled = (n === 0);
    $('#btnSimulate').textContent = n === 0 ? 'Run simulation' : `Run simulation (${n} change${n === 1 ? '' : 's'})`;
  }

  async function runSimulate() {
    if (Object.keys(State.overrides).length === 0) {
      toast('No overrides to simulate', 'warn');
      return;
    }
    // Phase 5: loading-state feedback. The solver is typically sub-second
    // but LP_HORIZON_HOURS=48 + many overrides can push into the 2-3s range,
    // and a disabled button with "Simulating…" copy tells the user why.
    const btn = document.getElementById('btnSimulate');
    const originalLabel = btn ? btn.textContent : '';
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Simulating…';
      btn.classList.add('is-busy');
    }
    document.body.classList.add('is-simulating');
    try {
      const [sim, current] = await Promise.all([
        jsonFetch('/api/v1/workbench/simulate', { method: 'POST', body: { overrides: State.overrides } }),
        jsonFetch('/api/v1/optimization/plan').catch(() => ({})),
      ]);
      State.lastSim = sim;
      State.currentPlan = current;
      switchTab('compare');
      renderCompare();
      if (!sim.ok) {
        toast(`Sim error: ${sim.error || sim.status || 'unknown'}`, 'bad');
      } else {
        toast(`Sim OK · objective £${(sim.objective_pence/100).toFixed(2)}`, 'ok');
      }
    } catch (e) {
      toast(`Simulate failed: ${e.message}`, 'bad');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove('is-busy');
        // Restore the label via the main refresh so N-change counter updates.
        btn.textContent = originalLabel;
        renderEditor();
      }
      document.body.classList.remove('is-simulating');
    }
  }

  function renderCompare() {
    const sim = State.lastSim;
    const cur = State.currentPlan;
    const summary = $('#compareSummary');
    if (!sim) { summary.innerHTML = ''; return; }

    let curObj = null;
    try {
      const lp = JSON.parse(cur?.lp?.lp_summary_json || '{}');
      curObj = typeof lp.objective_pence === 'number' ? lp.objective_pence : null;
    } catch (_e) {}
    const simObj = typeof sim.objective_pence === 'number' ? sim.objective_pence : null;
    const delta = (curObj != null && simObj != null) ? (simObj - curObj) : null;
    const deltaCls = delta == null ? '' : (delta > 0 ? 'text-bad' : (delta < 0 ? 'text-ok' : ''));
    summary.innerHTML = `
      <div class="insights-summary">
        <div class="insight-tile"><div class="label">Current obj.</div><div class="value">£${curObj == null ? '—' : (curObj/100).toFixed(2)}</div></div>
        <div class="insight-tile"><div class="label">Simulated obj.</div><div class="value">£${simObj == null ? '—' : (simObj/100).toFixed(2)}</div></div>
        <div class="insight-tile"><div class="label">Δ</div><div class="value ${deltaCls}">${delta == null ? '—' : (delta >= 0 ? '+' : '') + (delta/100).toFixed(2)}</div></div>
        <div class="insight-tile"><div class="label">Status</div><div class="value">${sim.status || '—'}</div></div>
      </div>
      ${sim.error ? `<p class="data-quality-note">⚠ ${sim.error}</p>` : ''}
    `;

    const tbody = $('#compareTable tbody');
    const slots = sim.slots || [];
    if (!slots.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-mute">No simulated slots returned.</td></tr>';
      return;
    }
    tbody.innerHTML = slots.map((s, i) => {
      // Slot labels in planner tz so Workbench matches Cockpit / Forecast / History.
      const lbl = (window.HEM && window.HEM.fmtSlotTime)
        ? window.HEM.fmtSlotTime(s.t)
        : (() => { const t = new Date(s.t); return `${String(t.getHours()).padStart(2,'0')}:${String(t.getMinutes()).padStart(2,'0')}`; })();
      const simImp = s.import_kwh ?? 0;
      // We don't have current per-slot import in the same shape; show — for now.
      return `<tr>
        <td>${lbl}</td>
        <td class="num">${(s.price_p == null ? '—' : Number(s.price_p).toFixed(1) + 'p')}</td>
        <td class="num text-mute">—</td>
        <td class="num">${Number(simImp).toFixed(2)}</td>
        <td class="num text-mute">—</td>
        <td class="num">${(s.soc_kwh == null ? '—' : Number(s.soc_kwh).toFixed(2))}</td>
        <td class="num text-mute">—</td>
      </tr>`;
    }).join('');
  }

  function switchTab(tab) {
    $$('.workbench-tab').forEach(t => t.classList.toggle('is-active', t.dataset.tab === tab));
    $$('.workbench-pane').forEach(p => p.classList.toggle('is-active', p.id === `pane${tab[0].toUpperCase()}${tab.slice(1)}`));
  }

  async function promote() {
    if (Object.keys(State.overrides).length === 0) {
      toast('Nothing to promote', 'warn');
      return;
    }
    const result = await wrapAction({
      simulateUrl: '/api/v1/workbench/promote/simulate',
      applyUrl: '/api/v1/workbench/promote',
      body: { overrides: State.overrides },
    });
    if (result.applied) {
      toast('Promoted', 'ok');
      // Re-fetch schema to refresh "current" values + clear pending state
      await loadSchema();
      State.overrides = {};
      renderEditor();
    }
  }

  async function loadSchema() {
    State.schema = await jsonFetch('/api/v1/workbench/schema');
    renderEditor();
  }

  async function loadProfiles() {
    try {
      const r = await jsonFetch('/api/v1/workbench/profiles');
      const sel = $('#profileSelect');
      sel.innerHTML = '<option value="">Load profile…</option>' +
        (r.profiles || []).map(p => `<option value="${p.name}">${p.name} (${p.key_count})</option>`).join('');
    } catch (_e) {}
  }

  async function loadProfile(name) {
    if (!name) return;
    try {
      const p = await jsonFetch(`/api/v1/workbench/profiles/${encodeURIComponent(name)}`);
      State.overrides = p.overrides || {};
      renderEditor();
      // Push values into inputs
      Object.entries(State.overrides).forEach(([k, v]) => {
        const el = $(`#${fieldId(k)}`);
        if (el) { el.value = v; el.classList.add('is-dirty'); }
      });
      repaintHeader();
      toast(`Loaded profile ${name}`, 'ok');
    } catch (e) {
      toast(`Load failed: ${e.message}`, 'bad');
    }
  }

  async function saveProfile() {
    const name = window.prompt('Profile name (letters, digits, _ - only):');
    if (!name) return;
    try {
      await jsonFetch(`/api/v1/workbench/profiles/${encodeURIComponent(name)}`, {
        method: 'POST', body: { overrides: State.overrides },
      });
      toast(`Saved ${name}`, 'ok');
      loadProfiles();
    } catch (e) {
      toast(`Save failed: ${e.message}`, 'bad');
    }
  }

  async function deleteProfile() {
    const name = $('#profileSelect').value;
    if (!name) { toast('Select a profile first', 'warn'); return; }
    if (!window.confirm(`Delete profile "${name}"?`)) return;
    try {
      await jsonFetch(`/api/v1/workbench/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
      toast(`Deleted ${name}`, 'ok');
      loadProfiles();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, 'bad');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('#btnSimulate').addEventListener('click', runSimulate);
    $('#btnPromote').addEventListener('click', promote);
    $('#btnSaveProfile').addEventListener('click', saveProfile);
    $('#btnDeleteProfile').addEventListener('click', deleteProfile);
    $('#profileSelect').addEventListener('change', e => loadProfile(e.target.value));
    $$('.workbench-tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
    loadSchema();
    loadProfiles();
  });
})();
