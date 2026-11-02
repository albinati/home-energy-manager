"""#941 -- command_failed channel + report_command_failure.

Owner rule: push ONLY when a hardware command is still broken after HEM's own
retry/recovery; a successful recovery is record-only (action_log)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db, notifier
from src.config import config
from src.daikin.client import DaikinError
from src.daikin.models import DaikinDevice
from src.foxess.client import FoxESSError
from src.foxess.models import SchedulerGroup

RCF = "src.notifier.report_command_failure"


@pytest.fixture()
def tdb(monkeypatch, tmp_path):
    p = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", p)
    monkeypatch.setattr(config, "DB_PATH", p, raising=False)
    db.init_db()
    return p


@pytest.fixture()
def transport(monkeypatch, tdb):
    sent: list[str] = []
    monkeypatch.setattr("src.telegram_transport.is_configured", lambda: True)
    monkeypatch.setattr(
        "src.telegram_transport.send_message",
        lambda text, **kw: sent.append(text) or True,
    )
    monkeypatch.setattr(config, "OPENCLAW_NOTIFY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_NOTIFY_TARGET", "chat", raising=False)
    return sent


@pytest.fixture()
def rcf(monkeypatch, tdb):
    m = MagicMock()
    monkeypatch.setattr(RCF, m)
    return m


def _recorded() -> list[str]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT params FROM action_log WHERE action = 'command_failed'"
        ).fetchall()
    return [str(tuple(r)) for r in rows]


# --- helper: delivery, recording, dedupe, seeding ---------------------------


def test_route_seeded_enabled_critical(tdb):
    r = db.get_notification_route("command_failed")
    assert r["enabled"] == 1 and r["severity"] == "critical"


def test_seed_keeps_existing_disabled_row_and_other_routes(tdb):
    with db.get_connection() as conn:
        conn.execute("DELETE FROM notification_routes WHERE alert_type='command_failed'")
        conn.execute(
            "INSERT INTO notification_routes (alert_type, enabled, severity, silent) "
            "VALUES ('command_failed', 0, 'reports', 1)"
        )
        conn.execute("UPDATE notification_routes SET enabled=0 WHERE alert_type='risk_alert'")
        conn.commit()
    db.init_db()
    r = db.get_notification_route("command_failed")
    assert (r["enabled"], r["severity"], r["silent"]) == (0, "reports", 1)
    assert db.get_notification_route("risk_alert")["enabled"] == 0


def test_headers_and_payload_name():
    assert notifier._telegram_header_for("command_failed") == "🛑 Comando não aplicado"
    assert notifier._payload_name_for_key("command_failed") == "EnergyCommandFailed"


def test_not_recovered_pushes_and_records(transport):
    notifier.report_command_failure(
        "k1", "Fox não aceitou o plano", recovered=False, device="foxess"
    )
    assert len(transport) == 1
    assert "Fox não aceitou o plano" in transport[0] and "foxess" in transport[0]
    assert "Comando não aplicado" in transport[0]
    assert _recorded()


def test_recovered_never_pushes_but_records(transport):
    notifier.report_command_failure("k2", "religado", recovered=True, device="daikin")
    assert transport == []
    assert _recorded()


def test_dedupe_per_kind_per_day(transport, monkeypatch):
    for _ in range(3):
        notifier.report_command_failure("k3", "x", recovered=False, device="foxess")
    assert len(transport) == 1
    notifier.report_command_failure("other", "y", recovered=False, device="foxess")
    assert len(transport) == 2
    # next local day: the key differs, so it pushes again (simulated by removing today's key)
    key_prefix = "command_failed_pushed:k3:"
    with db.get_connection() as conn:
        conn.execute("DELETE FROM kv_state WHERE key LIKE ?", (key_prefix + "%",))
        conn.commit()
    notifier.report_command_failure("k3", "x", recovered=False, device="foxess")
    assert len(transport) == 3


def test_dedupe_key_written_after_dispatch(monkeypatch, tdb):
    order: list[str] = []
    monkeypatch.setattr(notifier, "_dispatch", lambda *a, **k: order.append("dispatch"))
    real_set = db.set_kv
    monkeypatch.setattr(db, "set_kv", lambda k, v: (order.append("set_kv"), real_set(k, v)))
    notifier.report_command_failure("k4", "x", recovered=False, device="foxess")
    assert order == ["dispatch", "set_kv"]


def test_failed_dispatch_does_not_burn_the_dedupe_key(monkeypatch, tdb):
    def boom(*a, **k):
        raise RuntimeError("transport down")

    monkeypatch.setattr(notifier, "_dispatch", boom)
    with pytest.raises(RuntimeError):
        notifier.report_command_failure("k5", "x", recovered=False, device="foxess")
    assert not any(k.startswith("command_failed_pushed:k5") for k in _kv_keys())


def _kv_keys() -> list[str]:
    with db.get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT key FROM kv_state").fetchall()]


# --- wire tests: rerouted paths ---------------------------------------------


def _drift_env(monkeypatch):
    import src.state_machine as sm

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(config, "LWT_OFFSET_DRIFT_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "LWT_OFFSET_DRIFT_AUTO_RECOVER", True)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    sm._LWT_DRIFT_NOTIFIED = False
    return sm


def test_lwt_drift_reset_ok_is_record_only(monkeypatch, rcf):
    sm = _drift_env(monkeypatch)
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", lambda *a, **k: True)
    sm._check_lwt_offset_drift(
        [], MagicMock(), DaikinDevice(id="g", name="x", lwt_offset=3.0),
        datetime.now(UTC), trigger="t",
    )
    rcf.assert_called_once()
    assert rcf.call_args.args[0] == "lwt_offset_drift"
    assert rcf.call_args.kwargs["recovered"] is True


def test_lwt_drift_reset_failed_pushes(monkeypatch, rcf):
    sm = _drift_env(monkeypatch)

    def boom(*a, **k):
        raise DaikinError("429")

    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", boom)
    sm._check_lwt_offset_drift(
        [], MagicMock(), DaikinDevice(id="g", name="x", lwt_offset=3.0),
        datetime.now(UTC), trigger="t",
    )
    assert rcf.call_args.kwargs["recovered"] is False


def test_cold_backstop_write_failure_pushes_on_third_consecutive_tick(monkeypatch, rcf):
    from src.scheduler import lwt_coast

    assert lwt_coast.BACKSTOP_WRITE_FAIL_PUSH_TICKS == 3
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_ENABLED", True)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_TICKS", 1)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_MARGIN_C", 0.5)
    lwt_coast.reset_backstop()
    now = datetime.now(UTC)
    plan_date = now.date().isoformat()
    db.upsert_action(
        plan_date=plan_date,
        start_time=(now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        device="daikin", action_type="lwt_preheat",
        params={"lwt_offset": -2, "lp_optimizer": True}, status="active",
    )

    def boom(*a, **k):
        raise DaikinError("down")

    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params", boom)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 10.0})

    def tick():
        lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(),
                                client=MagicMock(), in_peak=False)

    tick(); tick()
    rcf.assert_not_called()
    tick()
    rcf.assert_called_once()
    assert rcf.call_args.kwargs["recovered"] is False
    assert rcf.call_args.args[0] == "lwt_cold_backstop_write"


def test_cold_backstop_success_is_record_only(monkeypatch, rcf):
    from src.scheduler import lwt_coast

    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_ENABLED", True)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_TICKS", 1)
    lwt_coast.reset_backstop()
    now = datetime.now(UTC)
    plan_date = now.date().isoformat()
    db.upsert_action(
        plan_date=plan_date,
        start_time=(now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        device="daikin", action_type="lwt_preheat",
        params={"lwt_offset": -2, "lp_optimizer": True}, status="active",
    )
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params", lambda *a, **k: True)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 10.0})
    out = lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(),
                                  client=MagicMock(), in_peak=False)
    assert out["fired"]
    assert rcf.call_args.kwargs["recovered"] is True


class _Fox:
    api_key = "k"

    def __init__(self, fail=()):
        self.fail = set(fail)

    def _do(self, name):
        if name in self.fail:
            raise FoxESSError(f"{name} rejected")

    def set_scheduler_flag(self, on):
        self._do("scheduler_flag")

    def set_work_mode(self, m):
        self._do("work_mode")

    def set_min_soc(self, v):
        self._do("min_soc_on_grid")


def test_survival_mode_pushes_and_fallback_failure_pushes(monkeypatch, rcf):
    from src.scheduler import octopus_fetch as of

    monkeypatch.setattr(of, "report_command_failure", rcf)

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    now = datetime(2026, 7, 8, 12, 34, tzinfo=UTC)
    of._maybe_survival_mode(_Fox(fail={"work_mode"}), 5, (now - timedelta(hours=25)).isoformat(), now)
    kinds = [c.args[0] for c in rcf.call_args_list]
    assert kinds == ["octopus_survival_mode", "octopus_survival_fox_fallback"]
    assert all(c.kwargs["recovered"] is False for c in rcf.call_args_list)


def test_apply_safe_defaults_pushes_once_summarising_legs(monkeypatch, rcf):
    from src.state_machine import apply_safe_defaults

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    apply_safe_defaults(_Fox(fail={"work_mode", "min_soc_on_grid"}), None, trigger="t")
    rcf.assert_called_once()
    assert rcf.call_args.kwargs["recovered"] is False
    detail = rcf.call_args.args[1]
    assert "work_mode" in detail and "min_soc_on_grid" in detail


def test_apply_safe_defaults_success_reports_nothing(monkeypatch, rcf):
    from src.state_machine import apply_safe_defaults

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    apply_safe_defaults(_Fox(), None, trigger="t")
    rcf.assert_not_called()


def _g(sh, sm_, eh, em, mode="ForceCharge"):
    return SchedulerGroup(start_hour=sh, start_minute=sm_, end_hour=eh, end_minute=em,
                          work_mode=mode, min_soc_on_grid=15, fd_soc=95, fd_pwr=3000)


def test_fox_overlap_refusal_pushes(monkeypatch, rcf):
    from src.scheduler.lp_dispatch import upload_fox_if_operational

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    fox = MagicMock()
    fox.api_key = "k"
    assert upload_fox_if_operational(fox, [_g(1, 0, 5, 59), _g(1, 0, 1, 59)]) is False
    rcf.assert_called_once()
    assert rcf.call_args.args[0] == "fox_upload_overlap_refused"
    assert rcf.call_args.kwargs["recovered"] is False


def test_fox_upload_failure_pushes_on_second_consecutive_and_success_resets(monkeypatch, rcf):
    from src.scheduler.lp_dispatch import upload_fox_if_operational

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(db, "save_fox_schedule_state", lambda *a, **k: None)
    fox = MagicMock()
    fox.api_key = "k"
    good = [_g(0, 30, 1, 59)]
    fox.set_scheduler_v3.side_effect = FoxESSError("41200")
    assert upload_fox_if_operational(fox, good) is False
    rcf.assert_not_called()
    assert upload_fox_if_operational(fox, good) is False
    rcf.assert_called_once()
    assert rcf.call_args.args[0] == "fox_upload_failed"
    assert rcf.call_args.kwargs["recovered"] is False
    # success resets the counter: the next single failure does not push
    fox.set_scheduler_v3.side_effect = None
    assert upload_fox_if_operational(fox, good) is True
    rcf.reset_mock()
    fox.set_scheduler_v3.side_effect = FoxESSError("41200")
    upload_fox_if_operational(fox, good)
    rcf.assert_not_called()


def _hw(enabled):
    hw = MagicMock()
    hw.enabled = enabled
    hw.groups = []
    return hw


def test_fox_flag_disabled_is_record_only_when_repair_ok(monkeypatch, rcf):
    from src.state_machine import heartbeat_repair_fox_scheduler

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    fox = MagicMock()
    fox.api_key = "k"
    fox.get_scheduler_flag.return_value = False
    fox.get_scheduler_v3.return_value = _hw(False)
    heartbeat_repair_fox_scheduler(fox)
    rcf.assert_called_once()
    assert rcf.call_args.kwargs["recovered"] is True
    fox.set_scheduler_flag.assert_called_with(True)


def test_fox_flag_repair_failure_pushes(monkeypatch, rcf):
    from src.state_machine import heartbeat_repair_fox_scheduler

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    fox = MagicMock()
    fox.api_key = "k"
    fox.get_scheduler_flag.return_value = False
    fox.get_scheduler_v3.return_value = _hw(False)
    fox.set_scheduler_flag.side_effect = FoxESSError("boom")
    heartbeat_repair_fox_scheduler(fox)
    recs = [c.kwargs["recovered"] for c in rcf.call_args_list]
    assert recs == [True, False]
    assert rcf.call_args_list[-1].args[0] == "fox_scheduler_repair_failed"
