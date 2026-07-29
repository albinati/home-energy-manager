"""The building UA goes from assumption to measurement exactly once, silently,
one autumn morning — unless something says so, and keeps saying it until it
lands.

Until then the house model is half-measured: τ comes from real overnight decay
episodes, but `fit_ua_hdd` needs 20 days with HDD > 1 and a British summer
supplies none, so `ua_source` stays NULL and every reader falls back to
`BUILDING_UA_W_PER_K`. C, derived as `τ × UA`, inherits the assumption too.

Verified against the prod DB on 2026-07-29: `ua_w_per_k` NULL, `ua_source` NULL,
`c_source` `tau_x_env_ua` — and the plumbing (24 usable days against a gate of
20) is already sufficient, so weather is the only thing left holding it back.

TESTING NOTE, earned the hard way in this feature's history. Stubbing `notify_*`
or `_dispatch` hides exactly the class of bug that has bitten here twice: a
signature mismatch that delivered nothing, and a transport that swallows outages
and returns False rather than raising. So the delivery tests stub only the ROUTE
lookup and the TRANSPORT, leaving every real signature and every real return
value in the path.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src import db
from src.analytics.thermal_learning import (
    _UA_ANNOUNCED,
    _UA_FIRST_FIT_KEY,
    _maybe_announce_first_ua_fit,
)
from src.config import config

_ROW = {
    "ua_w_per_k": 630.0, "ua_r2": 0.84, "ua_samples": 24, "ua_assumed_cop": 3.0,
    "tau_hours": 53.8, "c_kwh_per_k": 33.9, "ua_window_days": 30,
}
_OK = {"status": "ok"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 250.0, raising=False)
    db.init_db()
    yield


@pytest.fixture()
def transport(monkeypatch):
    """Capture at the transport edge. Everything above it — the helper's
    signature, `_dispatch`'s signature, its return value — stays real."""
    sent: list[str] = []
    monkeypatch.setattr("src.notifier._resolve_route", lambda k: {"silent": 0})
    monkeypatch.setattr("src.telegram_transport.is_configured", lambda: True)
    monkeypatch.setattr(
        "src.telegram_transport.send_message",
        lambda text, **kw: sent.append(text) or True,
    )
    return sent


# ---------------------------------------------------------------------------
# The transition
# ---------------------------------------------------------------------------


def test_the_transition_is_announced_and_reaches_the_transport(transport):
    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)

    assert len(transport) == 1, "the announcement never reached the transport"
    body = transport[0]
    assert "630" in body and "250" in body, "must carry what it replaces"
    assert "WINTER_THERMAL_MODEL" in body, "must point at what to compare against"
    assert db.get_runtime_setting(_UA_FIRST_FIT_KEY) == _UA_ANNOUNCED


def test_a_refit_is_not_news(transport):
    """The fit re-runs nightly all winter; only the first success is news."""
    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": 611.0}, row=_ROW, ua_fit=_OK)
    assert transport == []


def test_a_skipped_ua_fit_says_nothing(transport):
    """The summer case, every night for months."""
    _maybe_announce_first_ua_fit(
        prev={"ua_w_per_k": None}, row={"tau_hours": 53.8},
        ua_fit={"status": "skipped", "reason": "only 3 day(s) with HDD > 1.0"},
    )
    assert transport == []


def test_a_first_ever_row_with_no_prior_is_still_announced(transport):
    _maybe_announce_first_ua_fit(prev=None, row=_ROW, ua_fit=_OK)
    assert len(transport) == 1


# ---------------------------------------------------------------------------
# Delivery, and the retry the previous design only pretended to have
# ---------------------------------------------------------------------------


def test_a_swallowed_transport_failure_is_retried_on_a_later_run(monkeypatch):
    """The bug this replaces. `telegram_transport.send_message` swallows every
    network error and returns False, so the announcer could not detect an outage
    by catching exceptions — and marked the once-ever alert delivered anyway.

    Worse, the retry it imagined was unreachable: the calibration row is upserted
    BEFORE the announcement, so by the next run `prev["ua_w_per_k"]` is already
    set and the transition can never be detected twice. The state therefore has
    to live in `runtime_settings`, not in `prev`.
    """
    monkeypatch.setattr("src.notifier._resolve_route", lambda k: {"silent": 0})
    monkeypatch.setattr("src.telegram_transport.is_configured", lambda: True)
    monkeypatch.setattr("src.telegram_transport.send_message", lambda *a, **k: False)

    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)

    raw = db.get_runtime_setting(_UA_FIRST_FIT_KEY)
    assert raw and raw != _UA_ANNOUNCED, "an undelivered alert was marked delivered"
    assert json.loads(raw)["ua_w_per_k"] == 630.0, "the payload must survive the retry"

    sent: list[str] = []
    monkeypatch.setattr(
        "src.telegram_transport.send_message",
        lambda text, **kw: sent.append(text) or True,
    )
    # The next nightly run: `prev` now carries the value — exactly the state the
    # old design could not recover from.
    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": 630.0}, row=_ROW, ua_fit=_OK)

    assert len(sent) == 1, "the retry never happened"
    assert db.get_runtime_setting(_UA_FIRST_FIT_KEY) == _UA_ANNOUNCED

    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": 630.0}, row=_ROW, ua_fit=_OK)
    assert len(sent) == 1, "it announced twice"


def test_it_stays_announced_across_a_redeploy(transport):
    """This lands in October; an October deploy must not re-announce it. The
    state is in `runtime_settings`, not process memory."""
    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)
    assert len(transport) == 1
    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)
    assert len(transport) == 1


# ---------------------------------------------------------------------------
# What the message says
# ---------------------------------------------------------------------------


def test_the_cop_is_exonerated_only_when_it_actually_cancels(transport):
    """`fit_ua_hdd` computes `slope × assumed_cop` and the doc quotes 630 at
    COP 3 — so it cancels in the ratio ONLY when this fit used COP 3 too. At any
    other COP, claiming it cancels would steer the reader away from a genuine
    cause."""
    _maybe_announce_first_ua_fit(prev=None, row=_ROW, ua_fit=_OK)
    assert "é a explicação" in transport[0]

    db.set_runtime_setting(_UA_FIRST_FIT_KEY, "")
    _maybe_announce_first_ua_fit(
        prev=None, row={**_ROW, "ua_assumed_cop": 2.5}, ua_fit=_OK
    )
    body = transport[-1]
    assert "é a explicação" not in body, "the COP does not cancel at 2.5"
    assert "2.5" in body and "parte da diferença" in body


def test_the_window_comes_from_the_fit_not_a_hardcoded_number(transport):
    _maybe_announce_first_ua_fit(
        prev=None, row={**_ROW, "ua_window_days": 45}, ua_fit=_OK
    )
    assert "45 dias" in transport[0]


def test_tau_absent_prints_no_zeroes(transport):
    """A UA-only success on a fresh install leaves τ unset. "Com τ = 0 h …
    0.0 kWh/K" would be worse than saying nothing."""
    _maybe_announce_first_ua_fit(
        prev=None, row={**_ROW, "tau_hours": None, "c_kwh_per_k": None}, ua_fit=_OK
    )
    assert "capacidade térmica" not in transport[0]


# ---------------------------------------------------------------------------
# The nightly job must survive it
# ---------------------------------------------------------------------------


def test_an_announcement_failure_does_not_break_the_nightly_job(monkeypatch):
    """Runs the caller for real, rather than grepping its source."""
    import src.analytics.thermal_learning as tlm

    monkeypatch.setattr(
        tlm, "_maybe_announce_first_ua_fit",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    # The job bails early on an empty sensor table, so give it one reading.
    # `db` is imported inside the function, so patch the source module.
    monkeypatch.setattr(
        "src.db.get_indoor_readings_range",
        lambda *a, **k: [{"captured_at": "2026-07-01T00:00:00Z",
                          "room": "cozinha", "temp_c": 21.0}],
    )
    monkeypatch.setattr(tlm, "sanitize_phantom_heating", lambda *a, **k: ([], 0))
    monkeypatch.setattr(tlm, "select_decay_episodes", lambda *a, **k: [])
    monkeypatch.setattr(tlm, "fit_tau", lambda *a, **k: {
        "status": "ok", "tau_hours": 53.8, "r2_median": 0.97, "episodes": 13})
    monkeypatch.setattr(tlm, "_ua_fit_from_db", lambda *a, **k: {
        "status": "ok", "ua_w_per_k": 630.0, "r2": 0.84, "samples": 24,
        "assumed_cop": 3.0})
    monkeypatch.setattr(config, "THERMAL_LEARNING_ENABLED", True, raising=False)

    out = tlm.refresh_building_thermal_calibration()

    assert out.get("status") == "ok", "a failed announcement broke the job"
    row = db.get_building_thermal_calibration()
    assert row and row["ua_w_per_k"] == 630.0, "the calibration was not persisted"
