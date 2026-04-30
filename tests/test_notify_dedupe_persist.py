"""Notification dedupe state must survive container restarts.

2026-04-30 active-mode rollout fired duplicate "🔵 PAID to use" notifications
across three restarts inside the same negative-price window because
``_last_notified_slot_kind`` was a module-level Python global that resets on
every process boot. With persistence to ``runtime_settings``, the heartbeat
remembers the last announced slot kind across restarts and stays silent until
a real transition happens.
"""
from __future__ import annotations

import pytest

from src import db
from src.scheduler import runner as runner_mod


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets its own SQLite file so runtime_settings starts empty."""
    db_file = str(tmp_path / "dedupe.db")
    monkeypatch.setattr("src.config.config.DB_PATH", db_file)
    db.init_db()
    # Reset module-level state between tests so we don't leak loads.
    runner_mod._last_notified_slot_kind = None
    runner_mod._last_notified_slot_kind_loaded = False
    yield


def test_persisted_kind_loaded_on_first_tick():
    """A persisted "negative" kind from a prior run should be loaded on first
    heartbeat call so we don't re-announce the same slot kind."""
    db.set_runtime_setting("last_notified_slot_kind", "negative")
    # Simulate the lazy-load block from the heartbeat
    if not runner_mod._last_notified_slot_kind_loaded:
        persisted = db.get_runtime_setting("last_notified_slot_kind")
        if persisted:
            runner_mod._last_notified_slot_kind = persisted
        runner_mod._last_notified_slot_kind_loaded = True
    assert runner_mod._last_notified_slot_kind == "negative"


def test_transition_writes_to_runtime_settings():
    """When the heartbeat detects a transition, the new kind is persisted so a
    restart mid-window doesn't re-announce."""
    runner_mod._last_notified_slot_kind = None
    new_kind = "negative"
    if new_kind != runner_mod._last_notified_slot_kind:
        runner_mod._last_notified_slot_kind = new_kind
        db.set_runtime_setting("last_notified_slot_kind", new_kind)
    assert db.get_runtime_setting("last_notified_slot_kind") == "negative"


def test_no_persisted_value_survives_clean_state():
    """When runtime_settings has no row, lazy-load is a no-op and dedupe state
    starts as None (current first-boot behaviour, unchanged)."""
    assert db.get_runtime_setting("last_notified_slot_kind") is None
    runner_mod._last_notified_slot_kind_loaded = False
    if not runner_mod._last_notified_slot_kind_loaded:
        persisted = db.get_runtime_setting("last_notified_slot_kind")
        if persisted:
            runner_mod._last_notified_slot_kind = persisted
        runner_mod._last_notified_slot_kind_loaded = True
    assert runner_mod._last_notified_slot_kind is None


def test_full_cycle_simulates_restart_inside_negative_window(caplog):
    """End-to-end: enter negative window → transition fires → persisted →
    simulate restart (clear in-memory) → next tick reads persisted → NO
    second notification fires."""
    notifications: list[str] = []

    def _on_transition(new_kind: str):
        # Simulates the heartbeat's check `if slot_kind != _last_notified_slot_kind`
        if not runner_mod._last_notified_slot_kind_loaded:
            persisted = db.get_runtime_setting("last_notified_slot_kind")
            if persisted:
                runner_mod._last_notified_slot_kind = persisted
            runner_mod._last_notified_slot_kind_loaded = True
        if new_kind != runner_mod._last_notified_slot_kind:
            runner_mod._last_notified_slot_kind = new_kind
            db.set_runtime_setting("last_notified_slot_kind", new_kind)
            notifications.append(new_kind)

    # First heartbeat sees standard
    _on_transition("standard")
    assert notifications == ["standard"]

    # Slot transitions to negative — fires
    _on_transition("negative")
    assert notifications == ["standard", "negative"]

    # Container restart: in-memory state lost, runtime_settings still has "negative"
    runner_mod._last_notified_slot_kind = None
    runner_mod._last_notified_slot_kind_loaded = False

    # Next tick still inside negative — should NOT fire again
    _on_transition("negative")
    assert notifications == ["standard", "negative"], (
        f"duplicate ping after restart: {notifications}"
    )

    # When we eventually leave negative — fires for the new kind
    _on_transition("standard")
    assert notifications == ["standard", "negative", "standard"]
