"""LP health monitor: nightly regression self-check, alerts only on regression.

Covers the two regression signals (infeasible spike; battery discharging during
negative-price slots = #607 bug back) and the once-per-signature-per-day dedup.
"""
from __future__ import annotations

from src.config import config
from src.scheduler import runner


def _ev(**kw):
    base = dict(infeasible_24h=0, neg_slot_count=0, neg_discharge_kwh=0.0,
               max_infeasible=5, neg_discharge_thr=0.5)
    base.update(kw)
    return runner._evaluate_lp_health(**base)


def test_healthy_returns_no_issues():
    assert _ev() == []
    # negatives present but battery held (discharge ~0) → healthy
    assert _ev(neg_slot_count=10, neg_discharge_kwh=0.05) == []
    # a couple of infeasibles below threshold → healthy
    assert _ev(infeasible_24h=4) == []


def test_infeasible_spike_flagged():
    issues = _ev(infeasible_24h=8)
    assert len(issues) == 1 and "infeasible" in issues[0].lower()


def test_negative_discharge_flagged():
    issues = _ev(neg_slot_count=6, neg_discharge_kwh=2.5)
    assert len(issues) == 1 and "NEGATIVO" in issues[0]


def test_both_flagged():
    issues = _ev(infeasible_24h=9, neg_slot_count=6, neg_discharge_kwh=2.5)
    assert len(issues) == 2


def test_negative_discharge_ignored_without_negative_slots():
    # discharge high but no negative slots that day → not a #607 regression
    assert _ev(neg_slot_count=0, neg_discharge_kwh=5.0) == []


def test_floor_insurance_breach_flagged():
    issues = _ev(floor_insurance_24h_pence=300.0)
    assert len(issues) == 1 and "seguro" in issues[0]
    # below threshold → healthy
    assert _ev(floor_insurance_24h_pence=80.0) == []


def test_floor_slack_breach_flagged():
    issues = _ev(floor_slack_max_kwh=0.6)
    assert len(issues) == 1 and "slack" in issues[0]
    assert _ev(floor_slack_max_kwh=0.1) == []


# --- job-level: alerts once, dedup, silent when healthy ---

class _FakeConn:
    def __init__(self, infeasible, neg_slot_times):
        self._inf = infeasible
        self._neg = neg_slot_times
    def execute(self, sql, params=()):
        s = sql.lower()
        class _R:
            def __init__(self, rows): self._rows = rows
            def fetchone(self): return self._rows[0]
            def fetchall(self): return self._rows
        # #611 review follow-up: the infeasible count keys off the STRUCTURED
        # lp_inputs_snapshot.lp_status, not the strategy_summary display text.
        if "lp_inputs_snapshot" in s and "lp_status" in s:
            return _R([(self._inf,)])
        # PR B: pessimistic-charge-floor insurance/slack aggregation
        if "pess_charge_floor" in s:
            return _R([(0.0, 0.0)])
        if "agile_rates" in s:
            return _R([(f"2026-06-28T{t}:00Z",) for t in self._neg])
        return _R([])
    def close(self):  # closing(conn) in the job — no-op for the fake
        pass


class _FakeDB:
    _lock = __import__("threading").Lock()
    def __init__(self, *, infeasible=0, neg_slot_times=(), discharge=None, settings=None):
        self._conn = _FakeConn(infeasible, neg_slot_times)
        self._disch = discharge or {}
        self._settings = settings or {}
    def get_connection(self): return self._conn
    def half_hourly_battery_discharge_kwh_for_day(self, day): return self._disch
    def get_runtime_setting(self, k): return self._settings.get(k)
    def set_runtime_setting(self, k, v): self._settings[k] = v


def test_job_silent_when_healthy(monkeypatch):
    fake = _FakeDB(infeasible=0, neg_slot_times=(), discharge={})
    monkeypatch.setattr(runner, "db", fake)
    calls = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify_lp_health_regression", lambda issues: calls.append(issues))
    runner.bulletproof_lp_health_monitor_job()
    assert calls == [], "must not alert when healthy"


def test_job_alerts_on_negative_discharge_then_dedups(monkeypatch):
    # negative slots at 13:00/13:30 and the battery discharged there → regression
    fake = _FakeDB(
        infeasible=0, neg_slot_times=("13:00", "13:30"),
        discharge={"2026-06-28T13:00:00Z": 1.2, "2026-06-28T13:30:00Z": 1.0,
                   "2026-06-28T18:00:00Z": 0.8},  # 18:00 not a neg slot → excluded
    )
    monkeypatch.setattr(runner, "db", fake)
    calls = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify_lp_health_regression", lambda issues: calls.append(issues))

    runner.bulletproof_lp_health_monitor_job()
    assert len(calls) == 1, "should alert on negative-slot discharge"
    assert "NEGATIVO" in calls[0][0]
    assert fake._settings.get("lp_health_last_alert_sig")

    # same signature next run → no duplicate alert
    runner.bulletproof_lp_health_monitor_job()
    assert len(calls) == 1, "must dedup same-signature alert within the day"
