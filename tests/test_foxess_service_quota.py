"""Tests for src/foxess/service.py — quota-aware caching, stale fallback, force refresh."""
from unittest.mock import MagicMock, patch


def _reset_fox_service():
    import importlib

    import src.foxess.service as svc
    importlib.reload(svc)
    return svc


def _make_realtime(**kwargs):
    rt = MagicMock()
    rt.soc = kwargs.get("soc", 50.0)
    rt.solar_power = kwargs.get("solar_power", 2.0)
    rt.grid_power = kwargs.get("grid_power", 0.5)
    rt.battery_power = kwargs.get("battery_power", 1.0)
    rt.load_power = kwargs.get("load_power", 1.5)
    rt.work_mode = kwargs.get("work_mode", "Self Use")
    return rt


# ── Cache hit ────────────────────────────────────────────────────────────────

def test_cache_hit_no_api_call(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_fox_service()

    rt = _make_realtime()
    mock_client = MagicMock()
    mock_client.get_realtime.return_value = rt

    with patch("src.foxess.service._get_client", return_value=mock_client):
        with patch("src.foxess.service.record_call"):
            # Seed cache
            data1 = svc.get_cached_realtime(max_age_seconds=300)
            # Warm cache hit
            data2 = svc.get_cached_realtime(max_age_seconds=300)

    assert data1 is data2
    assert mock_client.get_realtime.call_count == 1


# ── Cache miss triggers API call ──────────────────────────────────────────────

def test_cache_miss_calls_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_fox_service()

    rt = _make_realtime()
    mock_client = MagicMock()
    mock_client.get_realtime.return_value = rt

    with patch("src.foxess.service._get_client", return_value=mock_client):
        with patch("src.foxess.service.record_call"):
            data = svc.get_cached_realtime(max_age_seconds=0)  # 0s TTL → always stale

    mock_client.get_realtime.assert_called_once()
    assert data.soc == 50.0


# ── Quota-blocked returns stale ───────────────────────────────────────────────

def test_quota_blocked_returns_stale_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_fox_service()

    rt = _make_realtime(soc=80.0)
    mock_client = MagicMock()
    mock_client.get_realtime.return_value = rt

    with patch("src.foxess.service._get_client", return_value=mock_client):
        with patch("src.foxess.service.record_call"):
            # Seed cache
            svc.get_cached_realtime(max_age_seconds=300)

    # Now block quota and force cache expiry
    svc._last_realtime_updated_monotonic = 0.0  # age = inf → expired
    with patch("src.foxess.service.should_block", return_value=True):
        data = svc.get_cached_realtime(max_age_seconds=300)

    # Should return stale cache (soc=80) without a new API call
    assert data.soc == 80.0
    assert mock_client.get_realtime.call_count == 1  # no extra call


# ── get_refresh_stats_extended ────────────────────────────────────────────────

def test_get_refresh_stats_extended_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_fox_service()

    stats = svc.get_refresh_stats_extended()
    for key in ("last_updated_epoch", "refresh_count_24h", "quota_used_24h",
                "quota_remaining_24h", "daily_budget", "blocked",
                "last_blocked_at", "cache_age_seconds", "stale"):
        assert key in stats, f"Missing key: {key}"


# ── force_refresh throttle ────────────────────────────────────────────────────

def test_force_refresh_throttled(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    svc = _reset_fox_service()

    rt = _make_realtime()
    mock_client = MagicMock()
    mock_client.get_realtime.return_value = rt

    with patch("src.foxess.service._get_client", return_value=mock_client):
        with patch("src.foxess.service.record_call"):
            with patch("src.foxess.service.should_block", return_value=False):
                # Seed cache
                svc.get_cached_realtime(max_age_seconds=300)

                # First force refresh — allowed
                svc._force_refresh_timestamps = {}
                svc.force_refresh_realtime("ui")

                # Second immediately — throttled (min_interval default = 60s)
                try:
                    result = svc.force_refresh_realtime("ui")
                    # Returned stale cache (not raised)
                    assert result is not None
                except Exception:
                    pass  # FoxESSError on cold-start throttle is also acceptable

    # Still only 2 calls total (cold-start seed + first force refresh)
    assert mock_client.get_realtime.call_count == 2
