"""Operations-status endpoints for the cockpit alert strip + self-check panel.

Two read-only, viewer-accessible aggregates designed for the UI's
reaction-time loop (see the ops-cockpit plan):

* ``GET /api/v1/status/alerts`` — binary health signals: meter freshness,
  LP failures, forecast provenance/degradation, Fox schedule drift, API
  quotas. One 60s-TTL poll powers the whole alert strip; every upstream
  read that could cost vendor quota sits behind its own LONGER sub-cache so
  client count can never amplify into live vendor calls.
* ``GET /api/v1/status/feedback`` — "are the changes working": DHW budget
  vs measured (auto-scale state), the LWT pre-heat demand gate, forecast
  provenance. 300s TTL.

Caching follows the in-process pattern of ``_period_insights_cache`` in
``main.py`` (#507): module-level dict, monotonic-free wall-clock TTL —
single-process app, cheap and sufficient.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter

from ... import db
from ...config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["status"])

# ── tiny TTL caches ──────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, Any]] = {}

ALERTS_TTL_S = 60
FEEDBACK_TTL_S = 300
SIDECAR_TTL_S = 300
FOX_DRIFT_TTL_S = 1800  # live Fox read costs quota — 48/day worst case, ~4% of budget


# Single-flight guard for the quota-costing computes: without it, N requests
# arriving on a cold cache would EACH fire a live vendor read before the
# first writes the cache (review L on #553) — the lock makes the "client
# count can never amplify into vendor calls" guarantee real.
_compute_locks: dict[str, asyncio.Lock] = {}


async def _cached_async(key: str, ttl: float, compute):
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    lock = _compute_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-check under the lock — a concurrent waiter may have filled it.
        hit = _cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ttl:
            return hit[1]
        out = await compute()
        _cache[key] = (now, out)
        return out


# ── building blocks ──────────────────────────────────────────────────────────

def _meter_block() -> dict[str, Any]:
    """Octopus meter freshness — the #533 alarm, on a UI surface at last."""
    last = db.get_octopus_meter_last_day()
    if last is None:
        return {"last_day": None, "age_days": None, "stale": True}
    try:
        age = (date.today() - date.fromisoformat(last)).days
    except ValueError:
        return {"last_day": last, "age_days": None, "stale": True}
    threshold = int(getattr(config, "CONSUMPTION_METER_STALE_DAYS", 3))
    return {"last_day": last, "age_days": age, "stale": threshold > 0 and age > threshold}


def _lp_block() -> dict[str, Any]:
    """Recent LP failures. error_class ONLY — never the stacktrace (this is a
    viewer-readable surface; stacktraces stay in the admin Journal/DB)."""
    rows = db.list_recent_lp_failures(20)
    now = datetime.now(UTC)
    recent = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["run_at_utc"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (KeyError, ValueError, TypeError):
            continue
        if (now - ts).total_seconds() <= 24 * 3600:
            recent.append(r)
    last = recent[0] if recent else None
    return {
        "failures_24h": len(recent),
        "last_failure": None if last is None else {
            "run_at_utc": last.get("run_at_utc"),
            "error_class": last.get("error_class"),
            "plan_date": last.get("plan_date"),
        },
    }


def _forecast_block(sidecar_ok: bool | None) -> dict[str, Any]:
    """Provenance of the forecast the LP is planning on (#542 migration):
    healthy = quartz-open-site and fresh; anything else is degraded-visible."""
    meta = db.get_latest_forecast_snapshot_meta()
    model_name = (meta or {}).get("model_name")
    fetched = (meta or {}).get("forecast_fetch_at_utc")
    age_s: float | None = None
    if fetched:
        try:
            ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_s = max(0.0, (datetime.now(UTC) - ts).total_seconds())
        except (ValueError, TypeError):
            age_s = None
    degraded = (
        model_name != "quartz-open-site"
        or age_s is None
        or age_s > 7200
        or sidecar_ok is False
    )
    return {
        "model_name": model_name,
        "source": (meta or {}).get("source"),
        "fetched_at_utc": fetched,
        "age_s": None if age_s is None else round(age_s),
        "sidecar_ok": sidecar_ok,
        "degraded": degraded,
    }


def _probe_sidecar_blocking() -> bool | None:
    """GET {QUARTZ_OPEN_URL}/health with a hard 1.5s timeout. ``None`` when
    the open provider isn't configured (nothing to probe)."""
    if (getattr(config, "QUARTZ_PROVIDER", "open") or "").lower() != "open":
        return None
    base = (getattr(config, "QUARTZ_OPEN_URL", "") or "").rstrip("/")
    if not base:
        return None
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _sidecar_ok() -> bool | None:
    return await _cached_async(
        "sidecar", SIDECAR_TTL_S,
        lambda: asyncio.to_thread(_probe_sidecar_blocking),
    )


async def _fox_drift_block() -> dict[str, Any]:
    """Schedule-drift summary behind a 30-min sub-cache. The underlying
    endpoint performs a LIVE Fox read — the sub-cache (not client courtesy)
    is what guarantees the alert strip can't burn quota."""
    async def compute() -> dict[str, Any]:
        try:
            from .dispatch import get_foxess_schedule_diff
            diff = await get_foxess_schedule_diff()
            # Contract: schedule_diff returns {"diffs": {"only_live": [...],
            # "only_recorded": [...]}} — review HIGH on #553 caught this
            # reading a nonexistent "differences" key (count was always 0).
            diffs = diff.get("diffs") or {}
            n_diff = len(diffs.get("only_live") or []) + len(diffs.get("only_recorded") or [])
            live_error = diff.get("live_error")
            # A failed live read makes the structural comparison meaningless
            # (empty live vs recorded reads as "drift") — report UNKNOWN
            # rather than caching a false alarm for 30 minutes.
            return {
                "checked_at_utc": datetime.now(UTC).isoformat(),
                "in_sync": None if live_error else not bool(diff.get("any_drift")),
                "diff_count": None if live_error else n_diff,
                "error": live_error,
            }
        except Exception as e:  # pragma: no cover — defensive: strip must render
            logger.warning("status/alerts: fox drift check failed: %s", e)
            return {"checked_at_utc": datetime.now(UTC).isoformat(),
                    "in_sync": None, "diff_count": None, "error": str(e)}
    return await _cached_async("fox_drift", FOX_DRIFT_TTL_S, compute)


def _quota_block() -> dict[str, Any]:
    out: dict[str, Any] = {"fox": None, "daikin": None}
    try:
        from ...foxess.service import get_refresh_stats_extended
        fx = get_refresh_stats_extended()
        out["fox"] = {
            "used": fx.get("quota_used_today_utc") or fx.get("quota_used_24h"),
            "budget": fx.get("daily_budget"),
            "blocked": bool(fx.get("blocked")),
        }
    except Exception:  # pragma: no cover
        logger.debug("status/alerts: fox quota read failed", exc_info=True)
    try:
        from ...daikin import service as daikin_service
        dq = daikin_service.get_quota_status_daikin()
        out["daikin"] = {
            "used": dq.get("quota_used_today_utc") or dq.get("quota_used_24h"),
            "budget": dq.get("daily_budget"),
            "blocked": bool(dq.get("blocked")),
        }
    except Exception:  # pragma: no cover
        logger.debug("status/alerts: daikin quota read failed", exc_info=True)
    return out


def _age_hours(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return max(0.0, (now - t).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return None


def _actuation_block() -> dict[str, Any]:
    """Is the plan actually reaching the hardware? The ~41h Fox-upload wedge of
    2026-06-14 ran silently because nothing watched actuation freshness (drift
    detection only compares live-vs-stored, both of which were stale-consistent).

    Fox: stale when the last SUCCESSFUL upload is older than the daily cadence.
    Daikin tank: stale when no tank action has fired in that window (~2×/day
    normally) + a recent device-rejection count. Daikin LWT: rejection count
    only (demand-gated, legitimately dormant in summer → no age alarm)."""
    now = datetime.now(UTC)
    since = (now - timedelta(hours=24)).isoformat()
    try:
        raw = db.get_actuation_health(since)
    except Exception:  # pragma: no cover — strip must still render
        logger.debug("status/alerts: actuation health read failed", exc_info=True)
        return {"fox": None, "daikin_tank": None, "daikin_lwt": None}

    fox_stale_h = float(getattr(config, "FOX_UPLOAD_STALE_HOURS", 30) or 0)
    tank_stale_h = float(getattr(config, "DAIKIN_TANK_STALE_HOURS", 30) or 0)
    fail_thr = max(1, int(getattr(config, "DAIKIN_FAILED_ALERT_THRESHOLD", 3) or 3))

    fox_age = _age_hours(raw.get("fox_upload_at"), now)
    tank_age = _age_hours(raw.get("tank_last_at"), now)
    tank_fail = int(raw.get("tank_failed_24h") or 0)
    lwt_fail = int(raw.get("lwt_failed_24h") or 0)

    # In vacation mode dhw_policy writes ZERO tank rows by design, so the tank
    # naturally goes "stale" with no fault — suppress the age alarm using the
    # SAME source dhw_policy reads, so the two can never disagree. (Failures
    # stay live: a rejected write is meaningful in any mode.)
    dhw_mode = (getattr(config, "OPTIMIZATION_PRESET", "normal") or "normal").strip().lower()
    tank_age_alarm = tank_stale_h > 0 and dhw_mode != "vacation"

    return {
        "fox": {
            "last_upload_at": raw.get("fox_upload_at"),
            "age_hours": None if fox_age is None else round(fox_age, 1),
            "stale": fox_stale_h > 0 and (fox_age is None or fox_age > fox_stale_h),
        },
        "daikin_tank": {
            "last_at": raw.get("tank_last_at"),
            "age_hours": None if tank_age is None else round(tank_age, 1),
            "failed_24h": tank_fail,
            "stale": tank_age_alarm and (tank_age is None or tank_age > tank_stale_h),
            "failing": tank_fail >= fail_thr,
        },
        "daikin_lwt": {
            "failed_24h": lwt_fail,
            # No age alarm — LWT is demand-gated and dormant in summer.
            "failing": lwt_fail >= fail_thr,
        },
    }


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/v1/status/alerts")
async def status_alerts() -> dict[str, Any]:
    """Binary health signals for the cockpit alert strip (viewer-readable).

    Healthy systems return all-quiet values and the strip renders nothing.
    """
    hit = _cache.get("alerts")
    now = time.time()
    if hit and now - hit[0] < ALERTS_TTL_S:
        return hit[1]

    sidecar = await _sidecar_ok()
    # Every sqlite reader goes through to_thread — _forecast_block included:
    # it takes db._lock, and holding that on the event loop during a large
    # scheduler write would stall every in-flight request (review M on #553).
    meter, lp, quota, forecast, actuation = await asyncio.gather(
        asyncio.to_thread(_meter_block),
        asyncio.to_thread(_lp_block),
        asyncio.to_thread(_quota_block),
        asyncio.to_thread(_forecast_block, sidecar),
        asyncio.to_thread(_actuation_block),
    )
    out = {
        "now_utc": datetime.now(UTC).isoformat(),
        "meter": meter,
        "lp": lp,
        "forecast": forecast,
        "fox_drift": await _fox_drift_block(),
        "quota": quota,
        "actuation": actuation,
    }
    _cache["alerts"] = (now, out)
    return out


@router.get("/api/v1/status/feedback")
async def status_feedback() -> dict[str, Any]:
    """The "are the changes working" panel (viewer-readable):
    DHW budget vs measured + auto-scale factor (#534), the LWT pre-heat
    demand gate (#540 quick win), and forecast provenance (#542).
    PV accuracy is NOT duplicated here — ``/api/v1/pv/today`` already
    serves MAE/bias and the UI polls it.
    """
    hit = _cache.get("feedback")
    now = time.time()
    if hit and now - hit[0] < FEEDBACK_TTL_S:
        return hit[1]

    def compute() -> dict[str, Any]:
        from ...dhw_policy import dhw_budget_state
        from ...scheduler.lp_dispatch import space_heating_gate_state
        return {
            "now_utc": datetime.now(UTC).isoformat(),
            "dhw": dhw_budget_state(),
            "lwt_gate": space_heating_gate_state(),
        }

    sidecar = await _sidecar_ok()
    out = await asyncio.to_thread(compute)
    out["forecast"] = await asyncio.to_thread(_forecast_block, sidecar)
    _cache["feedback"] = (now, out)
    return out
