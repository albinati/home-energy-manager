"""Guests-elevation detector: sustained base-load elevation → prompt the user.

Robust to noise (requires a sustained signal, not a single high day), one-shot
per episode (re-arms only when the signal normalises), and never fires when
already in guests mode. NEVER auto-applies.
"""
from __future__ import annotations

from src.config import config
from src.scheduler import runner


def _ev(daily, **kw):
    base = dict(window_days=4, ratio_thr=1.15, min_days=3, day_over=1.10, rearm_ratio=1.05)
    base.update(kw)
    return runner._evaluate_guests_signal(daily, **base)


def test_fires_on_sustained_elevation():
    # 4 days, actual ~30% above forecast on each → elevated
    daily = [(20.0, 15.0), (21.0, 16.0), (22.0, 16.0), (20.0, 15.0)]
    s = _ev(daily)
    assert s["elevated"] is True
    assert s["days_over"] == 4
    assert s["ratio"] > 1.15


def test_silent_on_single_day_noise():
    # one high day, three normal → not sustained
    daily = [(15.0, 15.0), (15.0, 15.0), (28.0, 15.0), (15.0, 15.0)]
    s = _ev(daily)
    assert s["elevated"] is False
    assert s["days_over"] == 1


def test_not_enough_data():
    daily = [(20.0, 15.0), (21.0, 16.0)]  # only 2 days, window 4
    s = _ev(daily)
    assert s["elevated"] is False and s["normalized"] is False
    assert s["n"] == 2


def test_normalized_rearm_signal():
    # signal back to ~normal → normalized True (re-arm)
    daily = [(15.0, 15.0), (14.5, 15.0), (15.2, 15.0), (14.8, 15.0)]
    s = _ev(daily)
    assert s["normalized"] is True
    assert s["elevated"] is False


def test_uses_only_trailing_window():
    # old elevated days dropped; trailing 4 are normal
    daily = [(30.0, 15.0)] * 3 + [(15.0, 15.0)] * 4
    s = _ev(daily)
    assert s["elevated"] is False  # trailing 4 are flat


# --- job-level: one-shot dedup, skip-when-guests, re-arm ---

class _FakeDB:
    def __init__(self, rows, armed="true"):
        self._rows = rows
        self._settings = {"guests_suggestion_armed": armed}
    def get_load_error_log_range(self, *a, **k):
        return self._rows
    def get_runtime_setting(self, key):
        return self._settings.get(key)
    def set_runtime_setting(self, key, value):
        self._settings[key] = value


def _rows_for_days(daily):
    """Two slots per day so the job's by-day aggregation reproduces the daily totals."""
    out = []
    for i, (a, f) in enumerate(daily):
        day = f"2026-06-{17 + i:02d}"
        out.append({"slot_time_utc": f"{day}T18:00:00Z", "actual_kwh": a / 2, "forecast_kwh": f / 2})
        out.append({"slot_time_utc": f"{day}T18:30:00Z", "actual_kwh": a / 2, "forecast_kwh": f / 2})
    return out


def test_job_fires_once_then_disarms(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    fake = _FakeDB(_rows_for_days([(20.0, 15.0)] * 4), armed="true")
    monkeypatch.setattr(runner, "db", fake)
    calls = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify_guests_mode_suggested", lambda **k: calls.append(k))

    runner.bulletproof_guests_detector_job()
    assert len(calls) == 1, "should suggest once on sustained elevation"
    assert fake._settings["guests_suggestion_armed"] == "false"

    # second run same episode → no re-fire (disarmed)
    runner.bulletproof_guests_detector_job()
    assert len(calls) == 1, "must not re-prompt during the same episode"


def test_job_skips_when_already_guests(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    fake = _FakeDB(_rows_for_days([(20.0, 15.0)] * 4), armed="true")
    monkeypatch.setattr(runner, "db", fake)
    calls = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify_guests_mode_suggested", lambda **k: calls.append(k))
    runner.bulletproof_guests_detector_job()
    assert calls == [], "must not suggest guests when already in guests mode"


def test_job_rearms_after_normalization(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    fake = _FakeDB(_rows_for_days([(15.0, 15.0)] * 4), armed="false")  # disarmed + normal signal
    monkeypatch.setattr(runner, "db", fake)
    runner.bulletproof_guests_detector_job()
    assert fake._settings["guests_suggestion_armed"] == "true", "should re-arm when signal normalises"
