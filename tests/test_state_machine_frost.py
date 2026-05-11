"""Daikin reconcile: hands-off climate guarantees on the reconcile path.

PR #300 (2026-05-09) established that HEM never writes climate-side fields
(lwt_offset, climate_on) to Daikin — firmware's own weather curve owns space
heating. This test pins the *defensive* stripping: even legacy
``action_schedule`` rows written before #300 (or by an external path) must
have those fields removed before reaching the Daikin client.

Replaces the prior frost-clamp tests, which expected the reconcile layer to
soften ``lwt_offset`` from -5 to -2 on cold days. With hands-off climate
that clamp is dead — there should be no ``lwt_offset`` reaching Daikin at
all. The 2026-05-11 incident showed a legacy row with ``lwt_offset=-5``
sabotaged DHW reheat even after a user override; stripping is the
correctness guarantee.
"""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.state_machine import reconcile_daikin_schedule_for_date


def _make_action(action_type: str = "shutdown") -> tuple[str, datetime]:
    """Insert an action_schedule row carrying polluted climate fields."""
    plan_date = "2030-06-01"
    now_utc = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
    st = (now_utc - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    en = (now_utc + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    db.upsert_action(
        plan_date=plan_date,
        start_time=st,
        end_time=en,
        device="daikin",
        action_type=action_type,
        params={"lwt_offset": -5.0, "climate_on": True,
                "tank_power": False, "tank_temp": 45.0},
        status="active",
    )
    return plan_date, now_utc


@pytest.mark.parametrize("outdoor_c", [0.0, 10.0])
def test_reconcile_strips_climate_fields_regardless_of_outdoor(
    monkeypatch: pytest.MonkeyPatch,
    outdoor_c: float,
) -> None:
    """lwt_offset and climate_on must be stripped on both cold and mild days."""
    captured: list[dict[str, Any]] = []

    def fake_apply(dev: Any, client: Any, params: dict[str, Any], **kw: Any) -> bool:
        captured.append(dict(params))
        return True

    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        fake_apply,
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        monkeypatch.setattr("src.config.config.WEATHER_FROST_THRESHOLD_C", 2.0)
        db.init_db()

        plan_date, now_utc = _make_action()

        class _Dev:
            pass

        class _Client:
            pass

        reconcile_daikin_schedule_for_date(
            plan_date,
            _Client(),
            _Dev(),
            now_utc,
            trigger="test",
            outdoor_c=outdoor_c,
        )
        assert captured, "apply_scheduled_daikin_params was not invoked"
        applied = captured[0]
        assert "lwt_offset" not in applied, (
            f"reconcile must strip lwt_offset (hands-off climate); got {applied!r}"
        )
        assert "climate_on" not in applied, (
            f"reconcile must strip climate_on (hands-off climate); got {applied!r}"
        )
        # Tank-side fields must still pass through.
        assert applied.get("tank_power") is False
        assert applied.get("tank_temp") == 45.0
