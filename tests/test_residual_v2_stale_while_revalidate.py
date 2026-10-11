"""residual_load_profile_v2 stale-while-revalidate + pv/today off-loop."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src import db
from src.api.routers import pv as pv_router


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    db.clear_residual_profile_cache()
    db._RESIDUAL_V2_REFRESHING.clear()
    import src.weather as weather
    monkeypatch.setattr(weather, "fetch_forecast", lambda hours=48: [])
    yield
    db.clear_residual_profile_cache()
    db._RESIDUAL_V2_REFRESHING.clear()


def _expire_all():
    for k, (_, v) in list(db._RESIDUAL_V2_CACHE.items()):
        db._RESIDUAL_V2_CACHE[k] = (time.monotonic() - 1.0, v)


def _wait_idle(timeout=10.0):
    end = time.monotonic() + timeout
    while db._RESIDUAL_V2_REFRESHING and time.monotonic() < end:
        time.sleep(0.01)
    assert not db._RESIDUAL_V2_REFRESHING


def test_allow_stale_returns_stale_and_single_refresh(monkeypatch):
    orig = db.residual_load_profile_v2
    first = orig()
    _expire_all()

    release = threading.Event()
    rebuilds: list[int] = []

    def wrapper(*a, **kw):
        if not kw.get("allow_stale"):  # the background rebuild
            rebuilds.append(1)
            release.wait(5)
        return orig(*a, **kw)

    monkeypatch.setattr(db, "residual_load_profile_v2", wrapper)

    # Two stale callers: both return immediately with the stale object.
    out = [wrapper(allow_stale=True), wrapper(allow_stale=True)]
    assert out[0] is first and out[1] is first
    time.sleep(0.1)
    assert len(rebuilds) == 1  # single-flight
    release.set()
    _wait_idle()
    assert len(rebuilds) == 1
    # Cache re-populated with a fresh (valid) entry.
    assert any(exp > time.monotonic() for exp, _ in db._RESIDUAL_V2_CACHE.values())


def test_allow_stale_without_entry_computes_synchronously():
    res = db.residual_load_profile_v2(allow_stale=True)
    assert "profile" in res
    assert not db._RESIDUAL_V2_REFRESHING


def test_default_path_recomputes_when_expired():
    first = db.residual_load_profile_v2()
    _expire_all()
    second = db.residual_load_profile_v2()
    assert second is not first  # fresh rebuild, no stale serving
    assert not db._RESIDUAL_V2_REFRESHING


def test_pv_today_does_not_block_event_loop(monkeypatch):
    def slow(**kw):
        time.sleep(0.5)
        return {"profile": {}, "spread": {}, "flat": 0.0}

    monkeypatch.setattr(db, "residual_load_profile_v2", slow)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        await pv_router.get_pv_today()
        t.cancel()
        return ticks

    # If the slow builder ran on the loop the ticker could not advance during it.
    assert asyncio.run(main()) >= 10
