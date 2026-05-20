"""``_mode_status_line`` must accurately reflect what HEM actually writes.

Background: PR #321 (2026-05-11 climate-strip incident) made HEM stop writing
``lwt_offset`` and ``climate_on`` to Daikin even in active mode. The brief's
active-mode label still claimed "HEM dispatches setpoints/LWT offset per LP
plan" — misleading anyone (LLM-renderer or human) reading the brief into
thinking HEM owns space heating. Issue #328 tracks this; fix is in this PR.

Both modes now correctly explain that the Daikin firmware's weather curve
owns space heating; the only active-mode difference is DHW tank target
dispatch.
"""
from __future__ import annotations

import pytest

from src.analytics import daily_brief
from src.config import config as app_config


def test_active_mode_says_tank_only_and_no_lwt_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active-mode label must (a) say HEM dispatches DHW tank only,
    (b) explicitly disclaim LWT offset writes (post-#321 truth)."""
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", False)
    line = daily_brief._mode_status_line()
    assert "active" in line
    assert "DHW tank" in line
    assert "firmware weather curve" in line
    assert "does NOT write LWT offset" in line
    # The old, misleading phrasing must be gone.
    assert "setpoints/LWT offset per LP plan" not in line


def test_passive_mode_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passive label was already correct; regression-pin it."""
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "passive")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", False)
    line = daily_brief._mode_status_line()
    assert "passive" in line
    assert "telemetry-only" in line
    assert "does NOT alter setpoints" in line


def test_read_only_overrides_fox_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENCLAW_READ_ONLY=true short-circuits Fox label regardless of mode."""
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)
    line = daily_brief._mode_status_line()
    assert "READ_ONLY" in line
    assert "no hardware writes" in line
