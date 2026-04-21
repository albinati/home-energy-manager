"""MPC vs Octopus fetch hour coalescing (#34)."""

from src.config import config as app_config
from src.scheduler.runner import mpc_should_skip_hour_for_octopus_fetch


def test_mpc_skip_when_local_hour_matches_octopus_fetch_hour(monkeypatch):
    monkeypatch.setattr(app_config, "OCTOPUS_FETCH_HOUR", 16)
    assert mpc_should_skip_hour_for_octopus_fetch(16) is True
    assert mpc_should_skip_hour_for_octopus_fetch(15) is False
    assert mpc_should_skip_hour_for_octopus_fetch(17) is False


def test_mpc_skip_respects_fetch_hour_zero(monkeypatch):
    monkeypatch.setattr(app_config, "OCTOPUS_FETCH_HOUR", 0)
    assert mpc_should_skip_hour_for_octopus_fetch(0) is True
    assert mpc_should_skip_hour_for_octopus_fetch(23) is False
