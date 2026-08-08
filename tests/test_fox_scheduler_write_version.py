"""Scheduler write route is version-switchable (#777).

Fox broke ``/op/v3/device/scheduler/enable`` for at least some devices on
2026-08-06: it answers ``41200 Failed to load data`` for every payload —
including the byte-identical group set the same device accepted hours earlier —
while v0/v1/v2 accept it and the v3 READ keeps working. Prod ran a two-day-old
single-group schedule for ~37 h before this surfaced, because each failed
upload left the previous schedule live.

These tests assert the WIRE: the URL actually POSTed and the exact JSON body,
not merely that some helper was called. The v2 body differs from v3 in two ways
that matter to the inverter — no ``isDefault``, and a per-group ``enable`` flag.
"""

import json

import pytest

from src.config import config
from src.foxess import client as client_mod
from src.foxess.client import FoxESSClient
from src.foxess.models import SchedulerGroup

GROUPS = [
    SchedulerGroup(2, 0, 3, 59, "Backup", min_soc_on_grid=10, max_soc=10),
    SchedulerGroup(12, 0, 13, 30, "ForceCharge", min_soc_on_grid=10, fd_soc=33, fd_pwr=5000),
]


class _Resp:
    """Minimal urlopen response carrying a success envelope."""

    def read(self):
        return json.dumps({"errno": 0, "msg": "Operation successful", "result": {}}).encode()


@pytest.fixture
def wire(monkeypatch):
    """Capture (url, parsed body) of every POST the client makes."""
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(req, timeout=None):
        sent.append((req.full_url, json.loads(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(config, "FOX_WRITE_INTER_DELAY_SECONDS", 0.0)
    return sent


def _client() -> FoxESSClient:
    return FoxESSClient(api_key="key", device_sn="SN123")


def test_default_write_goes_to_v2_with_per_group_enable(wire, monkeypatch):
    monkeypatch.setattr(config, "FOX_SCHEDULER_WRITE_VERSION", "v2")
    _client().set_scheduler_v3(GROUPS, skip_if_equal=False)

    assert len(wire) == 1
    url, body = wire[0]
    assert url == "https://www.foxesscloud.com/op/v2/device/scheduler/enable"
    # v2 rejects isDefault and requires enable on every group.
    assert "isDefault" not in body
    assert [g["enable"] for g in body["groups"]] == [1, 1]
    assert body["deviceSN"] == "SN123"
    # The substantive schedule must survive the route change untouched.
    assert body["groups"][0]["startHour"] == 2
    assert body["groups"][0]["workMode"] == "Backup"
    assert body["groups"][0]["extraParam"] == {"minSocOnGrid": 10, "maxSoc": 10}
    assert body["groups"][1]["extraParam"] == {"minSocOnGrid": 10, "fdSoc": 33, "fdPwr": 5000}


def test_v3_override_restores_the_legacy_route_and_body(wire, monkeypatch):
    monkeypatch.setattr(config, "FOX_SCHEDULER_WRITE_VERSION", "v3")
    _client().set_scheduler_v3(GROUPS, is_default=True, skip_if_equal=False)

    url, body = wire[0]
    assert url == "https://www.foxesscloud.com/op/v3/device/scheduler/enable"
    assert body["isDefault"] is True
    # v3 groups carry no enable flag — adding one is a v2-ism.
    assert all("enable" not in g for g in body["groups"])


def test_signature_path_tracks_the_version_segment(monkeypatch):
    """The signed string is the full path, so it must follow the route.

    A v2 URL signed with the v3 path authenticates as a different request and
    Fox rejects it — this is the failure mode that would silently reintroduce
    41200 under a different cause.
    """
    signed: list[str] = []
    c = _client()
    real = c._open_headers
    monkeypatch.setattr(c, "_open_headers", lambda p: signed.append(p) or real(p))
    monkeypatch.setattr(config, "FOX_WRITE_INTER_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        client_mod.urllib.request, "urlopen", lambda req, timeout=None: _Resp()
    )

    monkeypatch.setattr(config, "FOX_SCHEDULER_WRITE_VERSION", "v2")
    c.set_scheduler_v3(GROUPS, skip_if_equal=False)
    assert signed == ["/op/v2/device/scheduler/enable"]


def test_read_route_is_independent_of_the_write_route(wire, monkeypatch):
    """Read and write versions are separate knobs.

    The write moved for #777 (v3 rejects it); the read moved for #778 (v3 hides
    disabled slots). They are different faults with different rollbacks, so a
    single var must not conflate them.
    """
    monkeypatch.setattr(config, "FOX_SCHEDULER_WRITE_VERSION", "v2")
    monkeypatch.setattr(config, "FOX_SCHEDULER_READ_VERSION", "v3")
    _client().get_scheduler_v3()

    url, _ = wire[0]
    assert url == "https://www.foxesscloud.com/op/v3/device/scheduler/get"
