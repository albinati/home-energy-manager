/* Shared timezone helpers (Phase 1 of cockpit rework).
 *
 * The cockpit was previously doing `new Date(iso).getHours()` on UTC slot
 * timestamps, which returns the BROWSER's local hour, not the planner's. If
 * the user's browser is not in `Europe/London` (VPN, travel, wrong TZ),
 * labels like "16:30" become wrong while the LP is still correctly planning
 * in Europe/London.
 *
 * Every cockpit JS module formatting a slot time should now call
 * `HEM.fmtSlotTime(iso)` or `HEM.fmtSlotRange(fromIso, toIso)`. Those
 * delegate to Intl.DateTimeFormat with the planner_tz returned by
 * `/api/v1/system/timezone`, cached for the lifetime of the page.
 */
(function () {
  'use strict';
  const { jsonFetch } = window.HEM || {};

  // One in-flight fetch, memoized once resolved.
  let _tzPromise = null;
  let _tz = null;  // { planner_tz, plan_push_tz, now_utc, now_local }

  function loadTz() {
    if (_tz) return Promise.resolve(_tz);
    if (_tzPromise) return _tzPromise;
    _tzPromise = (jsonFetch ? jsonFetch('/api/v1/system/timezone') : fetch('/api/v1/system/timezone').then(r => r.json()))
      .then(r => { _tz = r; return r; })
      .catch(() => {
        // Fallback so the page still renders if the endpoint fails.
        _tz = { planner_tz: 'Europe/London', plan_push_tz: 'UTC' };
        return _tz;
      });
    return _tzPromise;
  }

  function plannerTz() {
    return (_tz && _tz.planner_tz) || 'Europe/London';
  }

  function _fmt(iso, opts) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    try {
      return new Intl.DateTimeFormat(undefined, Object.assign({ timeZone: plannerTz() }, opts)).format(d);
    } catch (_e) {
      return iso;
    }
  }

  // Slot time like "16:30" in the planner tz.
  function fmtSlotTime(iso) {
    return _fmt(iso, { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  // Slot range like "16:30–17:00" in the planner tz.
  function fmtSlotRange(fromIso, toIso) {
    return `${fmtSlotTime(fromIso)}–${fmtSlotTime(toIso)}`;
  }

  // Full local timestamp, e.g. "24/04/2026, 16:30:12".
  function fmtLocal(iso) {
    return _fmt(iso, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
  }

  // Produce a Date that represents "now in the planner tz but with local clock"
  // — useful for hour-of-day bucket math without leaking the browser tz. Returns
  // a Date whose Y/M/D/H/M fields match the planner clock; callers should use
  // those fields (getFullYear/getHours etc.) rather than the raw timestamp.
  function nowInPlannerTz() {
    const now = new Date();
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: plannerTz(),
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).formatToParts(now).reduce((acc, p) => { acc[p.type] = p.value; return acc; }, {});
    return new Date(
      `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`
    );
  }

  // Hour + minute of the planner-tz clock for a given UTC ISO stamp.
  function slotHM(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return { hour: 0, minute: 0 };
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: plannerTz(),
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(d);
    const hour = Number(parts.find(p => p.type === 'hour')?.value || 0);
    const minute = Number(parts.find(p => p.type === 'minute')?.value || 0);
    return { hour, minute };
  }

  window.HEM = window.HEM || {};
  window.HEM.Tz = {
    loadTz,
    plannerTz,
    fmtSlotTime,
    fmtSlotRange,
    fmtLocal,
    nowInPlannerTz,
    slotHM,
  };

  // Shortcuts used widely.
  window.HEM.fmtSlotTime = fmtSlotTime;
  window.HEM.fmtSlotRange = fmtSlotRange;

  // Kick off the fetch eagerly so callers who don't await loadTz() still
  // converge quickly. Any early call formats in the fallback tz (Europe/London)
  // which is correct for this single-home deployment anyway.
  document.addEventListener('DOMContentLoaded', () => {
    loadTz().then(tz => {
      const badge = document.getElementById('tzBadge');
      if (badge && tz && tz.planner_tz) {
        badge.textContent = `🕒 ${tz.planner_tz}`;
      }
    });
  });
})();
