"""Global test isolation — force every test run to use a per-session tmp DB.

Why this exists (#46):
Historical test runs leaked rows into the production SQLite database at
``/root/home-energy-manager/data/energy_state.db`` (71 rows at one point,
including 44 with hardcoded test plan_dates of 2030-06-01 / 2026-06-01, plus
27 malformed `status=active, executed_at=NULL` rows matching the test fixture
template in ``test_user_override.py::_seed_active_row``).

The leak happened because the pre-Phase 4 ``test_api_quota.py`` fixture called
``importlib.reload(src.config)`` + ``importlib.reload(src.api_quota)``. That
reload created a NEW ``Config`` instance inside reloaded modules while
``src.db`` still held a reference to the ORIGINAL ``Config`` instance via
its module-level ``from .config import config`` import. Subsequent tests that
did ``monkeypatch.setattr("src.config.config.DB_PATH", ...)`` mutated the
NEW instance — but ``src.db.get_connection()`` read from the OLD instance
and happily wrote to the production path.

The reload has been removed (Phase 4 review fix), but this conftest is
defense-in-depth so that:
1. Every test gets a per-session unique tmp DB path BEFORE any test-specific
   monkeypatch runs.
2. The ``DB_PATH`` env var is also pinned to the tmp path, so even tests that
   import ``src.config`` fresh (after a reload) see the isolated path as the
   class default.
3. The isolation fires whether or not the individual test remembers to set
   its own monkeypatch.

If a test explicitly wants to write to a specific DB path, it can still
override via its own ``monkeypatch.setattr("src.config.config.DB_PATH", ...)``
— pytest applies fixture-level monkeypatches first, then per-test patches on
top.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path_factory, monkeypatch):
    """Autouse — every test gets a fresh tmp DB path. Cannot be skipped."""
    tmp_db: Path = tmp_path_factory.mktemp("gstack-db") / "test.db"
    tmp_db_str = str(tmp_db)
    monkeypatch.setenv("DB_PATH", tmp_db_str)
    # Pin the attribute on the live Config instance — belt AND suspenders with
    # the env var, so callers that already read config.DB_PATH in-place still
    # see the isolated path.
    monkeypatch.setattr("src.config.config.DB_PATH", tmp_db_str)


@pytest.fixture(autouse=True)
def _default_daikin_active_for_tests(monkeypatch):
    """v10: production default for DAIKIN_CONTROL_MODE is 'passive'. Most tests
    were written before passive mode existed and assume the LP/dispatch can
    freely choose Daikin variables — i.e. active mode. Default tests to active
    so legacy assertions hold; tests covering passive behaviour explicitly
    override via ``monkeypatch.setenv("DAIKIN_CONTROL_MODE", "passive")``.
    """
    monkeypatch.setenv("DAIKIN_CONTROL_MODE", "active")
    from src.runtime_settings import clear_cache
    clear_cache()
