"""Daily Octopus Agile fetch → SQLite, retries, survival mode after 24h without rates."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import db
from ..config import config
from ..foxess.client import FoxESSClient, FoxESSError
from ..notifier import notify_critical
from .agile import fetch_agile_rates
from .optimizer import run_optimizer

logger = logging.getLogger(__name__)

_RETRY_DELAYS_SEC = [600, 1800, 3600]


def fetch_and_store_rates(fox: FoxESSClient | None = None) -> dict[str, Any]:
    """Fetch Agile rates, store in DB, run optimizer. Updates octopus_fetch_state."""
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    now = datetime.now(UTC)
    db.update_octopus_fetch_state(last_attempt_at=now.isoformat())

    # Opportunistically sync Fox ESS daily energy history for PV calibration
    if fox:
        try:
            _sync_fox_energy_history(fox)
        except Exception as e:
            logger.debug("Fox energy history sync (non-fatal): %s", e)

    if not tariff:
        return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}

    rates = fetch_agile_rates(tariff_code=tariff)
    if not rates:
        st = db.get_octopus_fetch_state()
        fails = st.consecutive_failures + 1
        streak = st.failure_streak_started_at or now.isoformat()
        db.update_octopus_fetch_state(
            consecutive_failures=fails,
            failure_streak_started_at=streak,
        )
        _maybe_survival_mode(fox, fails, streak, now)
        return {"ok": False, "error": "fetch failed", "consecutive_failures": fails}

    n = db.save_agile_rates(rates, tariff)
    db.update_octopus_fetch_state(
        last_success_at=now.isoformat(),
        consecutive_failures=0,
        survival_mode_since=None,
        clear_failure_streak=True,
    )

    summary: dict[str, Any] = {"ok": True, "rows": n}
    if config.USE_BULLETPROOF_ENGINE:
        try:
            # Compute plan and log to DB; device dispatch is handled by the nightly push job.
            opt = run_optimizer(fox if config.LP_MPC_WRITE_DEVICES else None)
            summary["optimizer"] = opt
            # notify_strategy_update removed — notify_plan_proposed in _write_plan_consent covers this
        except Exception as e:
            logger.exception("Optimizer after fetch failed: %s", e)
            summary["optimizer_error"] = str(e)

    return summary


def _maybe_survival_mode(
    fox: FoxESSClient | None,
    consecutive_failures: int,
    streak_started_iso: str,
    now: datetime,
) -> None:
    """After 24h without a successful fetch in this streak, lock Self Use and disable V3."""
    if consecutive_failures < 1:
        return
    st = db.get_octopus_fetch_state()
    if st.survival_mode_since:
        return
    try:
        t0 = datetime.fromisoformat(streak_started_iso.replace("Z", "+00:00"))
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=UTC)
    except ValueError:
        return
    if now - t0 < timedelta(hours=24):
        return

    db.update_octopus_fetch_state(survival_mode_since=now.isoformat())
    notify_critical(
        "Survival mode: no Octopus Agile rates for 24h in this failure streak. "
        "Fox ESS Scheduler V3 disabled; inverter set to Self Use until the next successful fetch."
    )
    if not fox or config.OPENCLAW_READ_ONLY or config.OPERATION_MODE != "operational":
        return
    try:
        if fox.api_key:
            fox.set_scheduler_flag(False)
        fox.set_work_mode("Self Use")
        fox.set_min_soc(10)
        db.log_action(
            device="foxess",
            action="survival_mode",
            params={"scheduler_flag": False},
            result="success",
            trigger="scheduler",
        )
    except FoxESSError as e:
        logger.warning("Survival mode Fox fallback failed: %s", e)


def next_retry_seconds(failures: int) -> int:
    if failures <= len(_RETRY_DELAYS_SEC):
        return _RETRY_DELAYS_SEC[max(0, failures - 1)]
    return 3600


def should_run_retry_fetch() -> bool:
    """True if we are in a failure streak and backoff elapsed since last attempt."""
    st = db.get_octopus_fetch_state()
    if st.consecutive_failures == 0 or not st.failure_streak_started_at:
        return False
    try:
        last = datetime.fromisoformat((st.last_attempt_at or "").replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
    except ValueError:
        last = datetime.now(UTC) - timedelta(days=1)
    delay = next_retry_seconds(st.consecutive_failures)
    return (datetime.now(UTC) - last).total_seconds() >= delay


def _sync_fox_energy_history(fox: FoxESSClient, months_back: int = 3) -> int:
    """Pull Fox ESS monthly daily energy breakdown and upsert into fox_energy_daily.

    v10.2: delegates to ``foxess.service.ensure_fox_month_cached`` which is
    SQLite-first — already-cached months are no-ops, only missing days fire
    a cloud call. The ``fox`` argument is kept for backward compat but unused
    (the service uses its own client singleton).

    Fetches the current month plus ``months_back`` prior months.
    Returns total rows present after sync (close to the historical 'rows
    upserted' figure for new installs).
    """
    from ..foxess import service as _fox_svc

    now = datetime.now(UTC)
    total = 0
    seen_months: set[tuple[int, int]] = set()

    for delta in range(months_back + 1):
        yr = now.year
        mo = now.month - delta
        while mo <= 0:
            mo += 12
            yr -= 1
        if (yr, mo) in seen_months:
            continue
        seen_months.add((yr, mo))
        try:
            rows = _fox_svc.ensure_fox_month_cached(yr, mo)
            total += len(rows)
            logger.debug("Fox energy sync %d-%02d via cache: %d rows", yr, mo, len(rows))
        except Exception as e:
            logger.debug("Fox energy sync %d-%02d failed: %s", yr, mo, e)

    return total
