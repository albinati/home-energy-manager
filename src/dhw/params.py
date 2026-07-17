"""The one place a learned value can enter the DHW model.

Everything downstream — the LP block, dispatch — asks :func:`resolve_tank_params`
for a :class:`~src.dhw.model.TankParams`. That is deliberate: it means there is
exactly one door through which a calibration can reach the physics, one place to
put the gates, and one line of log that says whether today's plan runs on measured
numbers or the databook.

The gates are conservative by construction. A fit has to clear its quality bar AND
be fresh; otherwise the databook value stands. Staleness matters as much as quality
here — a merge that never re-fits keeps its value forever, which is right for a quiet
week and wrong for a quiet season (a summer-fitted ambient must not steer a winter
plan). Past the age limit the reader falls back and says so.

Only UA and the ambient are ever learned. The COP is the certified curve baked into
:mod:`src.dhw.model`; the electrical caps are hardware; the resistance cliff is the
installer guide. None of those is a fit, and none of them belongs here.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from .model import TankParams

logger = logging.getLogger(__name__)

_MAX_AGE_DAYS_DEFAULT = 45.0


def _fresh(fitted_at_utc: str | None, max_age_days: float) -> bool:
    if not fitted_at_utc:
        return False
    try:
        ts = datetime.fromisoformat(str(fitted_at_utc).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return False
    age = (datetime.now(UTC) - ts).total_seconds() / 86400.0
    return age <= max_age_days


def resolve_tank_params() -> TankParams:
    """The tank's parameters for this solve: measured where they pass the gates,
    databook otherwise. Never raises — a calibration failure degrades to the
    databook tank, which is a perfectly good tank."""
    from .. import db

    databook = TankParams()  # the defaults ARE the databook + seed measurements

    enabled = True
    max_age = _MAX_AGE_DAYS_DEFAULT
    try:
        from ..config import config

        enabled = bool(getattr(config, "DHW_CALIBRATION_ENABLED", True))
        max_age = float(getattr(config, "DHW_CALIBRATION_MAX_AGE_DAYS", _MAX_AGE_DAYS_DEFAULT))
    except Exception:  # noqa: BLE001 — config outage must not break a solve
        pass
    if not enabled:
        return databook

    try:
        row = db.get_dhw_calibration("ua_ambient")
    except Exception:  # noqa: BLE001 — calibration must never break a solve
        return databook

    if not row or row.get("status") != "ok":
        return databook
    if not _fresh(row.get("fitted_at_utc"), max_age):
        logger.info("dhw.params: UA/ambient calibration is stale — using databook")
        return databook

    payload = row.get("payload") or {}
    ua = payload.get("ua_w_per_k")
    ambient = payload.get("ambient_c")
    if ua is None or ambient is None:
        return databook

    # The fit's own bounds already ran, but re-clamp at the door: the value about to
    # steer a real heat pump gets one last sanity check, independent of whoever wrote
    # the row.
    ua = float(ua)
    ambient = float(ambient)
    if not (1.0 <= ua <= 5.0) or not (10.0 <= ambient <= 28.0):
        logger.warning("dhw.params: learned UA=%.2f ambient=%.1f out of range — databook",
                       ua, ambient)
        return databook

    logger.info("dhw.params: using MEASURED tank — UA=%.2f W/K, ambient=%.1f °C (r2=%.2f, n=%s)",
                ua, ambient, row.get("r2") or 0.0, row.get("n_samples"))
    return TankParams(
        litres=databook.litres,
        cp_j_per_kg_k=databook.cp_j_per_kg_k,
        ua_w_per_k=ua,
        ambient_c=ambient,
        t_hp_max_c=databook.t_hp_max_c,
        hp_max_kw=databook.hp_max_kw,
        resistance_kw=databook.resistance_kw,
        source="measured",
    )


def resolve_reheat_differential_c() -> float:
    """The firmware's DHW reheat deadband for simulation/policy use (#732).

    Measured when the calibration passes its gates (status ok, fresh, in
    [2, 12] °C); otherwise the fallback — 6.0 °C, the robust threshold fitted over
    45 prod days (14/16 episodes explained; see #732). The old
    assumption of 1 °C made the baseline simulation top the tank up daily
    when the real firmware skips warm-tank days entirely.
    """
    from .. import db

    fallback = 6.0
    enabled = True
    max_age = _MAX_AGE_DAYS_DEFAULT
    try:
        from ..config import config

        fallback = float(getattr(config, "DHW_REHEAT_DIFFERENTIAL_FALLBACK_C", 6.0))
        enabled = bool(getattr(config, "DHW_CALIBRATION_ENABLED", True))
        max_age = float(getattr(config, "DHW_CALIBRATION_MAX_AGE_DAYS", _MAX_AGE_DAYS_DEFAULT))
    except Exception:  # noqa: BLE001 — config outage must not break a solve
        pass
    if not enabled:
        return fallback
    try:
        row = db.get_dhw_calibration("reheat_differential")
    except Exception:  # noqa: BLE001
        return fallback
    if not row or row.get("status") != "ok" or not _fresh(row.get("fitted_at_utc"), max_age):
        return fallback
    value = (row.get("payload") or {}).get("differential_c")
    if value is None:
        return fallback
    value = float(value)
    if not (2.0 <= value <= 12.0):
        logger.warning("dhw.params: learned reheat differential %.1f out of range — fallback", value)
        return fallback
    return value
