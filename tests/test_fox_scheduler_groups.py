"""Fox Scheduler V3 JSON → SchedulerGroup parsing."""
from __future__ import annotations

from src.foxess.client import scheduler_groups_from_stored_json


def test_scheduler_groups_from_stored_json_minimal() -> None:
    raw = [
        {
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 0,
            "workMode": "SelfUse",
            "extraParam": {"fdSoc": 92, "fdPwr": 5000},
        }
    ]
    groups = scheduler_groups_from_stored_json(raw)
    assert len(groups) == 1
    g = groups[0]
    assert g.start_hour == 2
    assert g.start_minute == 30
    assert g.end_hour == 5
    assert g.end_minute == 0
    assert g.fd_soc == 92
