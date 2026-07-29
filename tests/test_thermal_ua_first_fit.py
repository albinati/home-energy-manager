"""The building UA goes from assumption to measurement exactly once, silently,
one autumn morning — unless something says so.

Until then the house model is half-measured: τ comes from real overnight decay
episodes, but `fit_ua_hdd` needs 20 days with HDD > 1 and a British summer
supplies none, so `ua_source` stays NULL and every reader falls back to
`BUILDING_UA_W_PER_K`. C, derived as `τ × UA`, inherits the assumption too.

Verified against the prod DB on 2026-07-29: `ua_w_per_k` is NULL, `ua_source` is
NULL, `c_source` is `tau_x_env_ua` — and the data plumbing (24 usable days
against a gate of 20) is already sufficient, so weather is the only thing left
holding it back.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import db
from src.analytics.thermal_learning import (
    _UA_FIRST_FIT_KEY,
    _maybe_announce_first_ua_fit,
)
from src.config import config


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 250.0, raising=False)
    db.init_db()
    yield


_ROW = {
    "ua_w_per_k": 630.0, "ua_r2": 0.84, "ua_samples": 24,
    "ua_assumed_cop": 3.0, "tau_hours": 53.8, "c_kwh_per_k": 33.9,
}
_OK = {"status": "ok"}


def test_the_transition_from_assumption_to_measurement_is_announced(monkeypatch):
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_thermal_ua_first_fit", notify)

    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)

    notify.assert_called_once()
    kw = notify.call_args.kwargs
    assert kw["ua_w_per_k"] == 630.0
    assert kw["env_ua_w_per_k"] == 250.0, "must carry what it is replacing"
    assert kw["samples"] == 24 and kw["r2"] == 0.84


def test_a_refit_is_not_news(monkeypatch):
    """Only the first success is interesting. After that the fit re-runs nightly
    all winter and a ping per night would be noise."""
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_thermal_ua_first_fit", notify)

    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": 611.0}, row=_ROW, ua_fit=_OK)

    notify.assert_not_called()


def test_a_skipped_ua_fit_says_nothing(monkeypatch):
    """The summer case, every night for months."""
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_thermal_ua_first_fit", notify)

    _maybe_announce_first_ua_fit(
        prev={"ua_w_per_k": None}, row={"tau_hours": 53.8},
        ua_fit={"status": "skipped", "reason": "only 3 day(s) with HDD > 1.0"},
    )

    notify.assert_not_called()


def test_it_announces_once_even_across_a_redeploy(monkeypatch):
    """Deduped through `runtime_settings`, not process memory: this lands in
    October, and an October deploy must not re-announce it."""
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_thermal_ua_first_fit", notify)

    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)
    assert notify.call_count == 1
    assert db.get_runtime_setting(_UA_FIRST_FIT_KEY)

    # A fresh process (no in-memory state at all) with the same DB.
    _maybe_announce_first_ua_fit(prev={"ua_w_per_k": None}, row=_ROW, ua_fit=_OK)
    assert notify.call_count == 1


def test_a_first_ever_row_with_no_prior_is_still_announced(monkeypatch):
    """`prev` is None on a database that has never held a calibration row."""
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_thermal_ua_first_fit", notify)

    _maybe_announce_first_ua_fit(prev=None, row=_ROW, ua_fit=_OK)

    notify.assert_called_once()


def test_the_message_names_the_number_to_check_it_against(monkeypatch):
    """The whole point is that this is the ONLY moment the value is checkable.
    A ping that just states the result wastes the moment."""
    sent: list[str] = []
    monkeypatch.setattr(
        "src.notifier._dispatch",
        lambda alert, body, urgent=False, extra=None, **kw: sent.append(body),
    )
    from src.notifier import notify_thermal_ua_first_fit

    notify_thermal_ua_first_fit(
        ua_w_per_k=630.0, env_ua_w_per_k=250.0, r2=0.84, samples=24,
        assumed_cop=3.0, tau_hours=53.8, c_kwh_per_k=33.9,
    )

    body = sent[0]
    assert "630" in body and "250" in body
    assert "WINTER_THERMAL_MODEL" in body, "must point at what to compare against"
    assert "COP" in body, "must name the most likely culprit for a disagreement"


def test_a_notify_failure_does_not_break_the_nightly_job(monkeypatch):
    monkeypatch.setattr(
        "src.notifier.notify_thermal_ua_first_fit",
        MagicMock(side_effect=RuntimeError("telegram down")),
    )
    _maybe_announce_first_ua_fit(prev=None, row=_ROW, ua_fit=_OK)  # must not raise
