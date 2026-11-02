"""#939 -- notify_morning_tank_cold called _dispatch(body=...) (param is `message`)
and so raised TypeError on every call. These tests keep the real _dispatch
signature in the path and only stub the route lookup and the transport."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src import db, notifier
from src.config import config


@pytest.fixture()
def transport(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    db.init_db()
    sent: list[str] = []
    monkeypatch.setattr("src.telegram_transport.is_configured", lambda: True)
    monkeypatch.setattr(
        "src.telegram_transport.send_message",
        lambda text, **kw: sent.append(text) or True,
    )
    return sent


def test_morning_tank_cold_does_not_raise_and_is_recorded(transport):
    """Fresh DB seeds the route disabled: record-only, no push."""
    notifier.notify_morning_tank_cold(
        tank_c=38.0, floor_c=43.0, target_c=45.0, next_warmup_local="13:00"
    )
    assert transport == [], "route is record-only by default; nothing may be pushed"
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM action_log WHERE trigger = 'notification'"
        ).fetchall()
    assert any("morning_tank_cold" in str(tuple(r)) for r in rows)


def test_morning_tank_cold_route_seeded_disabled_and_existing_row_kept(transport):
    assert db.get_notification_route("morning_tank_cold")["enabled"] == 0
    with db.get_connection() as conn:
        conn.execute("UPDATE notification_routes SET enabled = 1 WHERE alert_type = 'morning_tank_cold'")
        conn.commit()
    db.init_db()
    assert db.get_notification_route("morning_tank_cold")["enabled"] == 1


def test_morning_tank_cold_has_hook_and_telegram_headers():
    key = notifier.AlertType.MORNING_TANK_COLD.value
    assert key in notifier._HOOK_PAYLOAD_NAMES
    assert key in notifier._TELEGRAM_HEADERS


def test_every_dispatch_call_in_src_uses_valid_keywords():
    sig = inspect.signature(notifier._dispatch)
    bad: list[str] = []
    n = 0
    for path in Path(notifier.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name != "_dispatch":
                continue
            n += 1
            where = f"{path.name}:{node.lineno}"
            if any(k.arg is None for k in node.keywords) or any(
                isinstance(a, ast.Starred) for a in node.args
            ):
                continue  # dynamic; cannot check statically
            try:
                sig.bind(*[0] * len(node.args), **{k.arg: 0 for k in node.keywords})
            except TypeError as exc:
                bad.append(f"{where}: {exc}")
    assert n >= 20, "AST scan found too few _dispatch calls; scan is broken"
    assert not bad, bad
