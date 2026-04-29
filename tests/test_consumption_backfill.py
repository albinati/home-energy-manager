"""Tests for the nightly Octopus-consumption backfill (V13)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


# ----------------------------------------------------------------------
# db.update_execution_log_metered — rewrite logic + slot-bucket matching
# ----------------------------------------------------------------------

def _seed_execution_row(timestamp_iso: str, *, agile=20.0, svt=24.0, fixed=22.0,
                       est_kwh=0.4, source="estimated"):
    """Insert a heartbeat-style row into execution_log."""
    from src import db
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO execution_log
               (timestamp, consumption_kwh, agile_price_pence,
                svt_shadow_price_pence, fixed_shadow_price_pence,
                cost_realised_pence, cost_svt_shadow_pence,
                cost_fixed_shadow_pence, delta_vs_svt_pence,
                delta_vs_fixed_pence, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp_iso, est_kwh, agile, svt, fixed,
                est_kwh * agile, est_kwh * svt, est_kwh * fixed,
                est_kwh * (svt - agile), est_kwh * (fixed - agile),
                source,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_row(timestamp_iso_prefix: str) -> dict | None:
    from src import db
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "SELECT * FROM execution_log WHERE timestamp LIKE ? LIMIT 1",
            (timestamp_iso_prefix + "%",),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def test_update_recomputes_all_cost_columns():
    """Given a heartbeat row at 13:32:14 with kwh=0.4 @ agile=20p, replacing
    with metered 1.5 kWh should rewrite all four cost columns."""
    from src import db

    _seed_execution_row("2026-04-29T13:32:14.123456+00:00", agile=20.0, svt=24.0, fixed=22.0, est_kwh=0.4)
    ok = db.update_execution_log_metered("2026-04-29T13:30:00+00:00", 1.5)
    assert ok is True

    row = _read_row("2026-04-29T13:32:14")
    assert row is not None
    assert row["consumption_kwh"] == 1.5
    assert row["cost_realised_pence"] == pytest.approx(1.5 * 20.0)
    assert row["cost_svt_shadow_pence"] == pytest.approx(1.5 * 24.0)
    assert row["cost_fixed_shadow_pence"] == pytest.approx(1.5 * 22.0)
    assert row["delta_vs_svt_pence"] == pytest.approx(1.5 * (24.0 - 20.0))
    assert row["delta_vs_fixed_pence"] == pytest.approx(1.5 * (22.0 - 20.0))
    assert row["source"] == "metered"


def test_update_matches_by_half_hour_bucket_not_exact_string():
    """The heartbeat writes ``timestamp`` at full microsecond precision
    wherever it fired in the slot. Octopus returns aligned slot starts.
    The matcher must find the row by half-hour bucket."""
    from src import db

    # Heartbeat fired at 13:32:14 — Octopus slot is 13:30:00.
    _seed_execution_row("2026-04-29T13:32:14.123456+00:00", est_kwh=0.4)

    # Backfill calls with the slot start.
    ok = db.update_execution_log_metered("2026-04-29T13:30:00+00:00", 0.9)
    assert ok is True
    row = _read_row("2026-04-29T13:32:14")
    assert row["consumption_kwh"] == 0.9


def test_update_does_not_cross_slot_boundary():
    """A heartbeat at 13:59:55 belongs to the [13:30, 14:00) bucket, NOT
    [14:00, 14:30). Backfill for 14:00 must NOT touch it."""
    from src import db

    _seed_execution_row("2026-04-29T13:59:55.000000+00:00", est_kwh=0.5)
    ok = db.update_execution_log_metered("2026-04-29T14:00:00+00:00", 9.99)
    assert ok is False
    row = _read_row("2026-04-29T13:59:55")
    assert row["consumption_kwh"] == 0.5  # untouched
    assert row["source"] == "estimated"


def test_update_returns_false_when_no_matching_row():
    from src import db

    ok = db.update_execution_log_metered("2026-04-29T13:30:00+00:00", 1.0)
    assert ok is False


def test_update_idempotent():
    """Re-running with the same kWh after a successful update is a no-op
    (well-defined: same final values, no cumulative drift)."""
    from src import db

    _seed_execution_row("2026-04-29T13:32:14.123456+00:00", agile=20.0, est_kwh=0.4)
    db.update_execution_log_metered("2026-04-29T13:30:00+00:00", 1.5)
    db.update_execution_log_metered("2026-04-29T13:30:00+00:00", 1.5)

    row = _read_row("2026-04-29T13:32:14")
    assert row["consumption_kwh"] == 1.5
    assert row["cost_realised_pence"] == pytest.approx(1.5 * 20.0)


def test_update_handles_malformed_timestamp_gracefully():
    from src import db

    assert db.update_execution_log_metered("not-an-iso-string", 1.0) is False


# ----------------------------------------------------------------------
# backfill_for_date — orchestration: Octopus fetch + per-slot rewrite
# ----------------------------------------------------------------------

@dataclass
class _FakeSlot:
    interval_start: datetime
    interval_end: datetime
    consumption_kwh: float


def test_backfill_for_date_rewrites_each_returned_slot(monkeypatch):
    """Given Octopus returns N slots, we should attempt N updates;
    successes get counted as ``slots_updated``, misses as ``slots_missing``."""
    from src.scheduler import consumption_backfill

    target = date(2026, 4, 28)

    # Seed three heartbeat rows for three different slot starts.
    _seed_execution_row("2026-04-28T22:01:30.000000+00:00", est_kwh=0.4)  # 22:00 UTC slot
    _seed_execution_row("2026-04-28T22:32:10.000000+00:00", est_kwh=0.4)  # 22:30 UTC slot
    # 23:00 UTC slot has NO heartbeat row — backfill should count it as missing.

    fake_slots = [
        _FakeSlot(
            interval_start=datetime(2026, 4, 28, 22, 0, tzinfo=UTC),
            interval_end=datetime(2026, 4, 28, 22, 30, tzinfo=UTC),
            consumption_kwh=0.85,
        ),
        _FakeSlot(
            interval_start=datetime(2026, 4, 28, 22, 30, tzinfo=UTC),
            interval_end=datetime(2026, 4, 28, 23, 0, tzinfo=UTC),
            consumption_kwh=1.20,
        ),
        _FakeSlot(
            interval_start=datetime(2026, 4, 28, 23, 0, tzinfo=UTC),
            interval_end=datetime(2026, 4, 28, 23, 30, tzinfo=UTC),
            consumption_kwh=0.75,
        ),
    ]
    fake_roles = MagicMock(import_mpan="2000000000000", import_serial="ABC123")

    # Patch the credential gate, the role resolver, and the API call.
    monkeypatch.setattr(consumption_backfill, "_octopus_credentials_ready", lambda: True)
    fake_octopus = MagicMock()
    fake_octopus.get_mpan_roles.return_value = fake_roles
    fake_octopus.fetch_consumption.return_value = fake_slots
    monkeypatch.setitem(__import__("sys").modules, "src.energy.octopus_client", fake_octopus)

    result = consumption_backfill.backfill_for_date(target)
    assert result.slots_fetched == 3
    assert result.slots_updated == 2
    assert result.slots_missing == 1
    assert result.error is None

    # Verify the rewrites landed.
    row22 = _read_row("2026-04-28T22:01:30")
    assert row22 is not None
    assert row22["consumption_kwh"] == 0.85
    assert row22["source"] == "metered"

    row2230 = _read_row("2026-04-28T22:32:10")
    assert row2230 is not None
    assert row2230["consumption_kwh"] == 1.20
    assert row2230["source"] == "metered"


def test_backfill_short_circuits_when_credentials_missing(monkeypatch):
    from src.scheduler import consumption_backfill

    monkeypatch.setattr(consumption_backfill, "_octopus_credentials_ready", lambda: False)
    result = consumption_backfill.backfill_for_date(date(2026, 4, 28))
    assert result.slots_updated == 0
    assert result.error == "octopus_credentials_missing"


def test_backfill_yesterday_picks_local_yesterday(monkeypatch):
    """Helper resolves "yesterday" via BULLETPROOF_TIMEZONE — verify the
    actual call goes to the right date."""
    from src.scheduler import consumption_backfill

    captured: list[date] = []

    def _fake_for_date(d: date):
        captured.append(d)
        return consumption_backfill.BackfillResult(
            target_date=d.isoformat(),
            slots_fetched=0, slots_updated=0, slots_missing=0,
        )

    monkeypatch.setattr(consumption_backfill, "backfill_for_date", _fake_for_date)
    consumption_backfill.backfill_yesterday()
    assert len(captured) == 1
    # Just verify it picked the day before "now local"; exact date depends on
    # when the test runs, but it's bound by ±1 day from UTC today.
    today = datetime.now(UTC).date()
    assert captured[0] in (today - timedelta(days=1), today, today - timedelta(days=2))
