/* v10.2 insights — Fox-ESS-style period browser.
 *
 * URL state: ?period=day|week|month|year&date=YYYY-MM-DD (or month=YYYY-MM /
 * year=YYYY). Keyboard shortcuts: ← → for prev/next, 1/7/30/Y to switch
 * granularity, T jumps to today.
 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const { jsonFetch, toast, PlanRender } = window.HEM || {};

  function fmtP(v) { return v == null ? '—' : `${Number(v).toFixed(1)}p`; }
  function fmtGBP(v) { return v == null ? '—' : `£${(Number(v) / 100).toFixed(2)}`; }
  function fmtKwh(v) { return v == null ? '—' : `${Number(v).toFixed(1)} kWh`; }
  function fmtPct(v) { return v == null ? '—' : `${Number(v).toFixed(1)}%`; }
  function pad(n) { return String(n).padStart(2, '0'); }

  function todayUTCDate() {
    const n = new Date();
    return new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate()));
  }

  const State = {
    period: 'month',
    cursor: todayUTCDate(),  // anchor date (for day/week) or 1st-of-month (for month/year)

    fromUrl() {
      const url = new URL(window.location.href);
      const p = url.searchParams.get('period');
      if (p && ['day', 'week', 'month', 'year'].includes(p)) this.period = p;
      const date = url.searchParams.get('date');
      const month = url.searchParams.get('month');
      const year = url.searchParams.get('year');
      if (date) {
        const d = new Date(date + 'T00:00:00Z');
        if (!isNaN(d)) this.cursor = d;
      } else if (month) {
        const d = new Date(month + '-01T00:00:00Z');
        if (!isNaN(d)) this.cursor = d;
      } else if (year) {
        const d = new Date(year + '-01-01T00:00:00Z');
        if (!isNaN(d)) this.cursor = d;
      }
    },

    toUrl(replace) {
      const url = new URL(window.location.href);
      url.searchParams.set('period', this.period);
      ['date', 'month', 'year'].forEach(k => url.searchParams.delete(k));
      if (this.period === 'day' || this.period === 'week') {
        url.searchParams.set('date', this.dateParam());
      } else if (this.period === 'month') {
        url.searchParams.set('month', this.monthParam());
      } else if (this.period === 'year') {
        url.searchParams.set('year', String(this.cursor.getUTCFullYear()));
      }
      const fn = replace ? 'replaceState' : 'pushState';
      window.history[fn]({}, '', url);
    },

    dateParam() {
      const c = this.cursor;
      return `${c.getUTCFullYear()}-${pad(c.getUTCMonth() + 1)}-${pad(c.getUTCDate())}`;
    },
    monthParam() {
      const c = this.cursor;
      return `${c.getUTCFullYear()}-${pad(c.getUTCMonth() + 1)}`;
    },

    apiParams() {
      const p = { period: this.period };
      if (this.period === 'day' || this.period === 'week') p.date = this.dateParam();
      else if (this.period === 'month') p.month = this.monthParam();
      else if (this.period === 'year') p.year = String(this.cursor.getUTCFullYear());
      return p;
    },

    label() {
      const c = this.cursor;
      const opts = { timeZone: 'UTC' };
      if (this.period === 'day') {
        return c.toLocaleDateString(undefined, { ...opts, weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' });
      }
      if (this.period === 'week') {
        const end = new Date(c); end.setUTCDate(end.getUTCDate() + 6);
        return `Week of ${c.toLocaleDateString(undefined, { ...opts, day: 'numeric', month: 'short' })} – ${end.toLocaleDateString(undefined, { ...opts, day: 'numeric', month: 'short', year: 'numeric' })}`;
      }
      if (this.period === 'month') return c.toLocaleDateString(undefined, { ...opts, month: 'long', year: 'numeric' });
      if (this.period === 'year') return String(c.getUTCFullYear());
      return '—';
    },

    nav(delta) {
      const c = new Date(this.cursor);
      if (this.period === 'day') c.setUTCDate(c.getUTCDate() + delta);
      else if (this.period === 'week') c.setUTCDate(c.getUTCDate() + 7 * delta);
      else if (this.period === 'month') c.setUTCMonth(c.getUTCMonth() + delta);
      else if (this.period === 'year') c.setUTCFullYear(c.getUTCFullYear() + delta);
      this.cursor = c;
    },

    setPeriod(p) {
      this.period = p;
      // For month/year, snap cursor to 1st-of-period for consistent URLs
      if (p === 'month') this.cursor = new Date(Date.UTC(this.cursor.getUTCFullYear(), this.cursor.getUTCMonth(), 1));
      if (p === 'year') this.cursor = new Date(Date.UTC(this.cursor.getUTCFullYear(), 0, 1));
    },
  };

  function repaintControls() {
    $$('.insights-period-btn').forEach(b => {
      const active = b.dataset.period === State.period;
      b.classList.toggle('btn-primary', active);
      b.classList.toggle('btn-secondary', !active);
    });
    $('#insightPeriodLabel').textContent = State.label();
    $('#dayViewSection').hidden = (State.period !== 'day');
    $('#dailyBreakdownSection').hidden = (State.period === 'day');
  }

  async function load() {
    repaintControls();
    State.toUrl(true);

    let d;
    try {
      const params = new URLSearchParams(State.apiParams());
      d = await jsonFetch(`/api/v1/energy/period?${params}`);
    } catch (e) {
      toast(`Insights: ${e.message}`, 'bad');
      return;
    }
    if (!d || d.detail) {
      toast(`Insights: ${d?.detail || 'no data'}`, 'bad');
      return;
    }

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

    // Daily breakdown (everything except day view)
    if (State.period !== 'day') {
      const tbody = $('#dailyTbody');
      const days = (d.chart_data || []).filter(r => (r.import_kwh || r.solar_kwh || r.load_kwh));
      if (!days.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-mute">No daily data for this period.</td></tr>';
      } else {
        tbody.innerHTML = days.map(r => `
            <tr>
              <td><a href="?period=day&date=${r.date}" data-deeplink="${r.date}">${r.date}</a></td>
              <td class="num">${(r.import_kwh ?? 0).toFixed(1)}</td>
              <td class="num">${(r.export_kwh ?? 0).toFixed(1)}</td>
              <td class="num">${(r.solar_kwh ?? 0).toFixed(1)}</td>
              <td class="num">${(r.load_kwh ?? 0).toFixed(1)}</td>
              <td class="num text-dim">${(r.charge_kwh ?? 0).toFixed(1)}</td>
              <td class="num text-dim">${(r.discharge_kwh ?? 0).toFixed(1)}</td>
            </tr>`).join('');
        // Intercept deep-link clicks so we don't full-reload
        $$('a[data-deeplink]', tbody).forEach(a => a.addEventListener('click', e => {
          e.preventDefault();
          State.cursor = new Date(a.dataset.deeplink + 'T00:00:00Z');
          State.setPeriod('day');
          load();
        }));
      }
    }

    // Day-view: tariff strip + slot table
    if (State.period === 'day') {
      try {
        const date = State.dateParam();
        const [tariff, exec, plan] = await Promise.all([
          jsonFetch(`/api/v1/agile/day?date=${date}`).catch(() => ({ slots: [] })),
          jsonFetch(`/api/v1/execution/today?date=${date}`).catch(() => ({ slots: [] })),
          jsonFetch('/api/v1/optimization/plan').catch(() => ({})),
        ]);
        PlanRender.renderTariffStrip({ mount: $('#tariffStrip'), tariff });
        PlanRender.renderSlotTable({
          table: $('#daySlotTable'),
          exec, plan,
          dataQualityNoteEl: $('#dayDataQualityNote'),
          totalsEl: $('#dayTotals'),
        });
      } catch (e) {
        toast(`Day view: ${e.message}`, 'bad');
      }
    }

    // Patterns (independent fetches; failures non-fatal)
    loadPatterns().catch(() => {});

    // Freshness line (E1.S2 cache metadata when available)
    const fresh = d.data_freshness;
    const freshEl = $('#insightFreshnessLine');
    if (Array.isArray(fresh) && fresh.length) {
      const oldest = fresh.reduce((a, b) => (b.age_seconds > a.age_seconds ? b : a), fresh[0]);
      const ageMin = Math.round((oldest.age_seconds || 0) / 60);
      freshEl.textContent = `Data age: oldest ${ageMin} min ago (${oldest.date}). Use ⟳ on Settings to refresh.`;
    } else {
      freshEl.textContent = '';
    }
  }

  function patternsRange() {
    const c = State.cursor;
    let start, end;
    if (State.period === 'day') {
      end = new Date(c);
      start = new Date(c); start.setUTCDate(start.getUTCDate() - 29);
    } else if (State.period === 'week') {
      end = new Date(c); end.setUTCDate(end.getUTCDate() + 6);
      start = new Date(c);
    } else if (State.period === 'month') {
      start = new Date(Date.UTC(c.getUTCFullYear(), c.getUTCMonth(), 1));
      end = new Date(Date.UTC(c.getUTCFullYear(), c.getUTCMonth() + 1, 0));
    } else {
      start = new Date(Date.UTC(c.getUTCFullYear(), 0, 1));
      end = new Date(Date.UTC(c.getUTCFullYear(), 11, 31));
    }
    const fmt = d => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    return { start: fmt(start), end: fmt(end) };
  }

  function renderBars(mount, items, { unit = '', max = null } = {}) {
    if (!mount) return;
    if (!items.length) {
      mount.innerHTML = '<p class="text-mute" style="font-size:0.78rem;">No data.</p>';
      return;
    }
    const cap = max ?? Math.max(0.001, ...items.map(i => Number(i.value) || 0));
    mount.innerHTML = items.map(i => {
      const pct = Math.min(100, 100 * (Number(i.value) || 0) / cap);
      return `<div class="pattern-bar-row">
          <span class="pattern-bar-label">${i.label}</span>
          <span class="pattern-bar-track"><span class="pattern-bar-fill" style="width:${pct.toFixed(1)}%"></span></span>
          <span class="pattern-bar-val">${(Number(i.value) || 0).toFixed(2)}${unit}</span>
      </div>`;
    }).join('');
  }

  async function loadPatterns() {
    const { start, end } = patternsRange();
    const qs = new URLSearchParams({ start, end });
    const [hourly, dow, dist, pv] = await Promise.all([
      jsonFetch(`/api/v1/patterns/hourly?${qs}`).catch(() => null),
      jsonFetch(`/api/v1/patterns/dow?${qs}`).catch(() => null),
      jsonFetch(`/api/v1/patterns/price-distribution?${qs}`).catch(() => null),
      jsonFetch(`/api/v1/patterns/pv-calibration?${qs}`).catch(() => null),
    ]);

    if (hourly) {
      const items = Object.entries(hourly.profile || {})
        .sort(([a], [b]) => Number(a) - Number(b))
        .map(([h, v]) => ({ label: `${pad(h)}h`, value: v.mean_kwh }));
      renderBars($('#patternHourly'), items);
    }
    if (dow) {
      const order = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      const items = order.map(d => ({ label: d, value: (dow.profile?.[d]?.mean_import_kwh) ?? 0 }));
      renderBars($('#patternDow'), items, { unit: ' kWh' });
    }
    if (dist) {
      const kinds = dist.kinds || {};
      const order = ['negative', 'cheap', 'standard', 'peak'];
      const total = dist.total_slots || 0;
      $('#patternPriceDist').innerHTML = total ? order.map(k => {
        const e = kinds[k] || { count: 0, pct: 0, mean_p: null };
        return `<div class="pattern-bar-row">
            <span class="pattern-bar-label kind-${k}">${k}</span>
            <span class="pattern-bar-track"><span class="pattern-bar-fill kind-${k}" style="width:${e.pct.toFixed(1)}%"></span></span>
            <span class="pattern-bar-val">${e.pct.toFixed(1)}% · ${e.count} · ${e.mean_p == null ? '—' : Number(e.mean_p).toFixed(1) + 'p'}</span>
        </div>`;
      }).join('') : '<p class="text-mute" style="font-size:0.78rem;">No tariff data in range.</p>';
    }
    if (pv) {
      const series = (pv.series || []).filter(s => s.actual_kwh != null);
      if (!series.length) {
        $('#patternPv').innerHTML = '<p class="text-mute" style="font-size:0.78rem;">No PV data in range.</p>';
      } else {
        const max = Math.max(...series.map(s => s.actual_kwh || 0)) || 1;
        $('#patternPv').innerHTML = `<div class="pv-spark">${series.map(s => {
          const h = Math.max(2, Math.round(40 * (s.actual_kwh || 0) / max));
          return `<div class="pv-spark-bar" style="height:${h}px" title="${s.date}: ${(s.actual_kwh || 0).toFixed(1)} kWh"></div>`;
        }).join('')}</div>`;
      }
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

  function bind() {
    $$('.insights-period-btn').forEach(b => b.addEventListener('click', () => {
      State.setPeriod(b.dataset.period);
      load();
    }));
    $$('.period-nav-btn').forEach(b => b.addEventListener('click', () => {
      const a = b.dataset.nav;
      if (a === 'prev') State.nav(-1);
      else if (a === 'next') State.nav(+1);
      else if (a === 'today') State.cursor = todayUTCDate();
      load();
    }));
    document.addEventListener('keydown', e => {
      if (e.target.matches('input, textarea, [contenteditable]')) return;
      if (e.key === 'ArrowLeft') { State.nav(-1); load(); }
      else if (e.key === 'ArrowRight') { State.nav(+1); load(); }
      else if (e.key === '1') { State.setPeriod('day'); load(); }
      else if (e.key === '7') { State.setPeriod('week'); load(); }
      else if (e.key.toLowerCase() === 'm') { State.setPeriod('month'); load(); }
      else if (e.key.toLowerCase() === 'y') { State.setPeriod('year'); load(); }
      else if (e.key.toLowerCase() === 't') { State.cursor = todayUTCDate(); load(); }
    });
    window.addEventListener('popstate', () => { State.fromUrl(); load(); });
    $('#btnRunCompare')?.addEventListener('click', runCompare);
  }

  document.addEventListener('DOMContentLoaded', () => {
    State.fromUrl();
    bind();
    load();
  });
})();
