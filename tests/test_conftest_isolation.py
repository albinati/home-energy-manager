"""Regression test for #46 — guarantees the autouse fixture in conftest.py
redirects every DB write to a tmp path.

Without this conftest, tests historically leaked rows into the production
SQLite database (default prod path: /root/home-energy-manager/data/energy_state.db)
because a pre-Phase 4 test_api_quota.py fixture used importlib.reload, which
produced two separate Config instances — tests monkeypatched one, src.db read
from the other.

Override the prod-path check via the ``HEM_PROD_DB_PATH`` env var for dev
hosts where the prod DB lives elsewhere (or not at all).

If this test ever fails, a change to conftest.py or the Config class has
broken the invariant. Fix before merging.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_PROD_DB_PATH = "/root/home-energy-manager/data/energy_state.db"


def _prod_db_path() -> Path:
    return Path(os.environ.get("HEM_PROD_DB_PATH", _DEFAULT_PROD_DB_PATH))


def test_autouse_fixture_redirects_config_db_path_to_tmp():
    """The conftest autouse fixture must set config.DB_PATH to a tmp dir per test."""
    from src.config import config
    db_path = Path(config.DB_PATH)
    # Not the prod path
    assert "data/energy_state.db" not in str(db_path), (
        f"config.DB_PATH still points at prod: {db_path}. "
        f"tests/conftest.py's autouse isolation is broken."
    )
    # Not anywhere obviously shared
    prod_dir = _prod_db_path().parent
    assert db_path.parent != prod_dir, (
        f"config.DB_PATH parent is the prod data dir: {db_path}"
    )


def test_upsert_action_writes_to_tmp_not_prod():
    """End-to-end: a db.upsert_action call goes to the tmp DB, not prod."""
    import sqlite3

    from src import db
    from src.config import config

    db.init_db()
    db.upsert_action(
        plan_date="2099-01-01",
        start_time="2099-01-01T00:00:00Z",
        end_time="2099-01-01T00:30:00Z",
        device="daikin",
        action_type="shutdown",
        params={"conftest_isolation_test": True},
        status="active",
    )

    # The row must exist in the TMP path
    tmp_conn = sqlite3.connect(config.DB_PATH)
    tmp_count = tmp_conn.execute(
        "SELECT COUNT(*) FROM action_schedule WHERE date='2099-01-01'"
    ).fetchone()[0]
    tmp_conn.close()
    assert tmp_count >= 1, "row did not land in the tmp DB that config.DB_PATH points to"

    # The row MUST NOT exist in the prod DB (only meaningful when the prod DB
    # is actually present and readable from this host — dev boxes skip this
    # half of the check).
    prod_path = _prod_db_path()
    try:
        prod_exists = prod_path.exists()
    except (PermissionError, OSError):
        return
    if not prod_exists:
        return
    prod_conn = sqlite3.connect(str(prod_path))
    prod_count = prod_conn.execute(
        "SELECT COUNT(*) FROM action_schedule "
        "WHERE date='2099-01-01' AND json_extract(params, '$.conftest_isolation_test')=1"
    ).fetchone()[0]
    prod_conn.close()
    assert prod_count == 0, (
        f"LEAK: test wrote {prod_count} conftest-isolation marker row(s) "
        f"to the production DB at {prod_path}. Tests are NOT isolated."
    )
