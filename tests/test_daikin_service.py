"""Tests for src/daikin/service.py — singleton cache, quota-aware refresh, slot window gate."""
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from zoneinfo import ZoneInfo

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_device(dev_id: str = "dev-1") -> MagicMock:
    dev = MagicMock()
    dev.id = dev_id
    return dev


def _reset_service():
    """Reset module-level state between tests."""
    import importlib

    import src.daikin.service as svc
    importlib.reload(svc)
    return svc


# ── Cold-start ────────────────────────────────────────────────────────────────

def test_cold_start_calls_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_service()

    mock_client = MagicMock()
    mock_client.get_devices.return_value = [_make_device()]

    with patch("src.daikin.service.DaikinClient", return_value=mock_client):
        result = svc.get_cached_devices(allow_refresh=False, actor="test")

    assert result.source == "cold_start"
    assert len(result.devices) == 1
    mock_client.get_devices.assert_called_once()


# ── Cache hit ────────────────────────────────────────────────────────────────

def test_cache_hit_does_not_call_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_service()

    mock_client = MagicMock()
    devices = [_make_device()]
    mock_client.get_devices.return_value = devices

    with patch("src.daikin.service.DaikinClient", return_value=mock_client):
        # First call: cold start
        svc.get_cached_devices(allow_refresh=False, actor="test")
        # Second call: should hit cache
        result = svc.get_cached_devices(allow_refresh=False, actor="test")

    assert result.source == "cache"
    assert not result.stale
    # get_devices called exactly once (cold start only)
    assert mock_client.get_devices.call_count == 1


# ── Stale cache, allow_refresh=False ─────────────────────────────────────────

def test_stale_cache_no_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_service()

    mock_client = MagicMock()
    mock_client.get_devices.return_value = [_make_device()]

    with patch("src.daikin.service.DaikinClient", return_value=mock_client):
        # Seed cache
        svc.get_cached_devices(allow_refresh=False, actor="test")
        # Force cache to appear expired. Setting to 0.0 only works when
        # ``time.monotonic()`` is large enough to be > TTL (true in long-lived
        # dev shells, false in short-lived CI runners). Subtract the TTL +
        # margin from current monotonic so age > TTL deterministically.
        import time as _time
        from src.config import config as _cfg
        svc._devices_fetched_monotonic = _time.monotonic() - (_cfg.DAIKIN_DEVICES_CACHE_TTL_SECONDS + 60)

        result = svc.get_cached_devices(allow_refresh=False, actor="test")

    assert result.source == "cache_stale"
    assert result.stale
    assert mock_client.get_devices.call_count == 1  # still no new call


# ── Quota-blocked refresh ─────────────────────────────────────────────────────

def test_quota_blocked_returns_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_service()

    mock_client = MagicMock()
    mock_client.get_devices.return_value = [_make_device()]

    with patch("src.daikin.service.DaikinClient", return_value=mock_client):
        # Seed cache
        svc.get_cached_devices(allow_refresh=False, actor="test")
        # Expire it deterministically — see test_stale_cache_no_refresh for
        # why ``= 0.0`` is not safe in short-lived CI runners.
        import time as _time
        from src.config import config as _cfg
        svc._devices_fetched_monotonic = _time.monotonic() - (_cfg.DAIKIN_DEVICES_CACHE_TTL_SECONDS + 60)

        # Block quota
        with patch("src.daikin.service.should_block", return_value=True):
            result = svc.get_cached_devices(allow_refresh=True, actor="test")

    assert result.stale
    assert result.source == "cache_stale"
    assert mock_client.get_devices.call_count == 1


# ── force_refresh throttle ────────────────────────────────────────────────────

def test_force_refresh_throttled(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_service()

    mock_client = MagicMock()
    mock_client.get_devices.return_value = [_make_device()]

    with patch("src.daikin.service.DaikinClient", return_value=mock_client):
        with patch("src.daikin.service.should_block", return_value=False):
            # Seed the cache
            svc.get_cached_devices(allow_refresh=False, actor="test")

            # First force refresh — allowed (no throttle yet)
            svc._force_refresh_timestamps = {}
            svc.force_refresh_devices("ui")

            # Second force refresh immediately — throttled
            result = svc.force_refresh_devices("ui")

    assert result.stale


# ── _in_octopus_pre_slot_window ───────────────────────────────────────────────

def test_pre_slot_window_at_boundaries():
    from src.scheduler.runner import _in_octopus_pre_slot_window

    # 25:00 into hour (= minute 25, second 0) → in window
    t_in_1 = datetime(2026, 4, 18, 12, 25, 0, tzinfo=UTC)
    assert _in_octopus_pre_slot_window(t_in_1, lead_seconds=300)

    # 29:59 into hour → in window
    t_in_2 = datetime(2026, 4, 18, 12, 29, 59, tzinfo=UTC)
    assert _in_octopus_pre_slot_window(t_in_2, lead_seconds=300)

    # 30:00 into hour → NOT in window (boundary itself)
    t_out_1 = datetime(2026, 4, 18, 12, 30, 0, tzinfo=UTC)
    assert not _in_octopus_pre_slot_window(t_out_1, lead_seconds=300)

    # 55:00 into hour → in window
    t_in_3 = datetime(2026, 4, 18, 12, 55, 0, tzinfo=UTC)
    assert _in_octopus_pre_slot_window(t_in_3, lead_seconds=300)

    # 00:00 exactly (next hour boundary) → NOT in window
    t_out_2 = datetime(2026, 4, 18, 13, 0, 0, tzinfo=UTC)
    assert not _in_octopus_pre_slot_window(t_out_2, lead_seconds=300)

    # 10:00 mid-slot → NOT in window
    t_out_3 = datetime(2026, 4, 18, 12, 10, 0, tzinfo=UTC)
    assert not _in_octopus_pre_slot_window(t_out_3, lead_seconds=300)


def test_daikin_calibration_window_at_boundaries():
    from src.scheduler.runner import _in_daikin_calibration_window

    tz = ZoneInfo("Europe/London")

    # Morning window start.
    assert _in_daikin_calibration_window(datetime(2026, 4, 18, 6, 0, 0, tzinfo=tz))
    # Morning window end boundary is excluded.
    assert not _in_daikin_calibration_window(datetime(2026, 4, 18, 8, 0, 0, tzinfo=tz))
    # Afternoon window start.
    assert _in_daikin_calibration_window(datetime(2026, 4, 18, 14, 30, 0, tzinfo=tz))
    # Afternoon window end boundary is excluded.
    assert not _in_daikin_calibration_window(datetime(2026, 4, 18, 16, 30, 0, tzinfo=tz))


def test_daikin_2h_refresh_window_fires_on_even_hour_first_minutes():
    """The 2h-aligned window fires for [02, 07) min past each even UTC hour.

    Onecta caches in 2-hour buckets; firing right after the rotation gives us
    the freshest data. 12 fires/day = 12 calls/day, well under 200/day quota.
    See #267 (S1).
    """
    from src.scheduler.runner import _in_daikin_2h_refresh_window

    # 00:02 UTC — start of window after 00:00 rotation → True.
    assert _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 0, 2, 0, tzinfo=UTC))
    # 00:06 UTC — still in [02, 07) → True.
    assert _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 0, 6, 59, tzinfo=UTC))
    # 00:07 UTC — exclusive end → False.
    assert not _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 0, 7, 0, tzinfo=UTC))
    # 00:01 UTC — before window start → False.
    assert not _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 0, 1, 59, tzinfo=UTC))


def test_daikin_2h_refresh_window_skips_odd_hours():
    """Odd-hour UTC ticks never fire — Onecta rotates on even hours only."""
    from src.scheduler.runner import _in_daikin_2h_refresh_window

    # 01:02, 03:04, 05:06 — all odd-hour, all False even within minute window.
    assert not _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 1, 2, 0, tzinfo=UTC))
    assert not _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 3, 4, 0, tzinfo=UTC))
    assert not _in_daikin_2h_refresh_window(datetime(2026, 5, 6, 5, 6, 0, tzinfo=UTC))


def test_daikin_2h_refresh_window_fires_12x_per_day():
    """Sanity: enumerating every minute of a day, the window fires for exactly
    12 distinct 5-minute blocks (one per even UTC hour)."""
    from src.scheduler.runner import _in_daikin_2h_refresh_window

    fires_per_hour: dict[int, int] = {}
    for hour in range(24):
        for minute in range(60):
            t = datetime(2026, 5, 6, hour, minute, 0, tzinfo=UTC)
            if _in_daikin_2h_refresh_window(t):
                fires_per_hour[hour] = fires_per_hour.get(hour, 0) + 1

    assert sorted(fires_per_hour.keys()) == list(range(0, 24, 2))  # 12 even hours
    assert all(n == 5 for n in fires_per_hour.values())  # 5 minutes each
