"""SQLite init smoke test (no hardware)."""
import tempfile
from pathlib import Path

import pytest

from src import db


def test_init_db_creates_file(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        assert path.exists()
