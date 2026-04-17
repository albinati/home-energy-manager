"""Daily Octopus Agile fetch → SQLite, retries, survival mode after 24h without rates."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import config
from .. import db
from ..notifier import notify_critical, notify_strategy_update
from ..foxess.client import FoxESSClient, FoxESSError
from .agile import fetch_agile_rates
from .optimizer import run_optimizer

logger = logging.getLogger(__name__)

_RETRY_DELAYS_SEC = [600, 1800, 3600]


def fetch_and_store_rates(fox: Optional[FoxESSClient] = None) -> dict[str, Any]:
    """Fetch Agile rates, store in DB, run optimizer. Updates octopus_fetch_state."""
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    now = datetime.now(timezone.utc)
    db.update_octopus_fetch_state(last_attempt_at=now.isoformat())

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
            opt = run_optimizer(fox)
            summary["optimizer"] = opt
            if opt.get("ok") and opt.get("strategy"):
                notify_strategy_update(str(opt.get("strategy")), warnings=opt.get("battery_warning"))
        except Exception as e:
            logger.exception("Optimizer after fetch failed: %s", e)
            summary["optimizer_error"] = str(e)

    return summary


def _maybe_survival_mode(
    fox: Optional[FoxESSClient],
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
            t0 = t0.replace(tzinfo=timezone.utc)
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
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        last = datetime.now(timezone.utc) - timedelta(days=1)
    delay = next_retry_seconds(st.consecutive_failures)
    return (datetime.now(timezone.utc) - last).total_seconds() >= delay
