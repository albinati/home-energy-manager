"""GET /api/v1/daikin/lwt-schedule — surfaces the committed lwt_preheat offset
rows (today+tomorrow) and excludes restores / non-LWT daikin rows (#481)."""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import config


def _local_today_iso() -> str:
    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    return datetime.now(tz).date().isoformat()


def _seed(conn, *, date: str, action_type: str, params: dict, start_h: int):
    start = f"{date}T{start_h:02d}:00:00Z"
    end = f"{date}T{start_h + 1:02d}:00:00Z"
    conn.execute(
        """INSERT INTO action_schedule
           (date, start_time, end_time, device, action_type, params, status, created_at)
           VALUES (?, ?, ?, 'daikin', ?, ?, 'pending', ?)""",
        (date, start, end, action_type, json.dumps(params), start),
    )
    conn.commit()


def test_lwt_schedule_returns_only_preheat_rows(monkeypatch):
    import src.db as db
    from src.api import main

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    today = _local_today_iso()

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed(conn, date=today, action_type="lwt_preheat", params={"lwt_offset": 3, "lp_optimizer": True}, start_h=10)
            _seed(conn, date=today, action_type="lwt_preheat", params={"lwt_offset": -2, "lp_optimizer": True}, start_h=15)
            # A restore (offset 0) and a tank row must NOT appear.
            _seed(conn, date=today, action_type="restore", params={"lwt_offset": 0}, start_h=16)
            _seed(conn, date=today, action_type="pre_heat", params={"tank_power": True, "tank_temp": 45}, start_h=12)
        finally:
            conn.close()

        resp = asyncio.run(main.daikin_lwt_schedule())

    assert resp["enabled"] is True
    rows = resp["rows"]
    assert len(rows) == 2, f"only the two lwt_preheat rows, got {rows}"
    assert all(r["action_type"] == "lwt_preheat" for r in rows)
    # Sorted by start time: boost (+3) then setback (-2).
    assert rows[0]["lwt_offset"] == 3
    assert rows[1]["lwt_offset"] == -2
    assert rows[0]["start_utc"] and rows[0]["end_utc"]


def test_lwt_schedule_disabled_flag(monkeypatch):
    import src.db as db
    from src.api import main

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", False, raising=False)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
        db.init_db()
        resp = asyncio.run(main.daikin_lwt_schedule())
    assert resp["enabled"] is False
    assert resp["rows"] == []
