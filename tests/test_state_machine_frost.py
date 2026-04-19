"""Daikin reconcile: live frost softening of peak LWT setback."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.state_machine import reconcile_daikin_schedule_for_date


def test_reconcile_shutdown_softens_lwt_when_cold_outdoor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        plan_date = "2030-06-01"
        now_utc = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
        st = (now_utc - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        en = (now_utc + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        db.upsert_action(
            plan_date=plan_date,
            start_time=st,
            end_time=en,
            device="daikin",
            action_type="shutdown",
            params={"lwt_offset": -5.0, "climate_on": True},
            status="active",
        )

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
            outdoor_c=0.0,
        )
        assert captured
        assert captured[0].get("lwt_offset") == -2.0


def test_reconcile_shutdown_keeps_scheduled_lwt_when_mild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        plan_date = "2030-06-01"
        now_utc = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
        st = (now_utc - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        en = (now_utc + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        db.upsert_action(
            plan_date=plan_date,
            start_time=st,
            end_time=en,
            device="daikin",
            action_type="shutdown",
            params={"lwt_offset": -5.0, "climate_on": True},
            status="active",
        )

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
            outdoor_c=10.0,
        )
        assert captured and captured[0].get("lwt_offset") == -5.0
