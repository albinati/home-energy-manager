"""Is the plan actually reaching the hardware?

Shared by the `/api/v1/status/alerts` block and the watchdog cron that pages on
it. Deliberately ONE implementation: the 2026-08-08 review found the MCP
schedule-diff tool had drifted from its REST twin and kept reporting healthy
through an outage, so a second copy of *this* logic — the thing that decides
whether to wake somebody at 3am — is exactly the copy we must not make.

Pure: takes the raw DB signals and a clock, returns the verdict. No I/O.

Background. Two wedges in two months, each ~37-41 h, each found by a human
happening to look at the cockpit:

* 2026-06-14 — Fox V3 upload wedged ~41 h. `db.get_actuation_health` was added
  in response, "after the ~41h Fox-upload wedge that nothing alerted on".
* 2026-08-06 — Fox broke the v3 write endpoint (#777). The inverter ran a
  two-day-old schedule for ~37 h; the battery stopped cycling entirely
  (measured export collapsed to 0.01-0.06 kWh/day against 1.2-5.3 either
  side). The freshness signal was correct the whole time. Nothing pushed it.

The June fix built the signal and left it in the UI. This module is the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def age_hours(ts: str | None, now: datetime) -> float | None:
    """Hours since an ISO timestamp; None when absent or unparseable."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return max(0.0, (now - t).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return None


def evaluate_actuation_health(
    raw: dict[str, Any],
    now: datetime,
    *,
    fox_stale_hours: float,
    tank_stale_hours: float,
    failed_threshold: int,
    dhw_mode: str,
) -> dict[str, Any]:
    """Verdict per actuation domain from `db.get_actuation_health` output.

    ``fox``: stale when the last SUCCESSFUL upload is older than the daily
    cadence — `save_fox_schedule_state` runs only on success, so a frozen value
    IS the failure signal.

    ``daikin_tank``: stale on age (fires ~2x/day under dhw_policy) plus a
    device-rejection count. In vacation mode dhw_policy writes zero tank rows by
    design, so the age alarm is suppressed using the same source dhw_policy
    reads — the two can never disagree. Rejections stay live in every mode: a
    refused write means something regardless of intent.

    ``daikin_lwt``: rejection count only. LWT is demand-gated and legitimately
    dormant in summer, so an age alarm there would cry wolf for months.
    """
    fox_age = age_hours(raw.get("fox_upload_at"), now)
    tank_age = age_hours(raw.get("tank_last_at"), now)
    tank_fail = int(raw.get("tank_failed_24h") or 0)
    lwt_fail = int(raw.get("lwt_failed_24h") or 0)
    fail_thr = max(1, int(failed_threshold or 3))

    tank_age_alarm = tank_stale_hours > 0 and (dhw_mode or "").strip().lower() != "vacation"

    return {
        "fox": {
            "last_upload_at": raw.get("fox_upload_at"),
            "age_hours": None if fox_age is None else round(fox_age, 1),
            "stale": fox_stale_hours > 0 and (fox_age is None or fox_age > fox_stale_hours),
        },
        "daikin_tank": {
            "last_at": raw.get("tank_last_at"),
            "age_hours": None if tank_age is None else round(tank_age, 1),
            "failed_24h": tank_fail,
            "stale": tank_age_alarm and (tank_age is None or tank_age > tank_stale_hours),
            "failing": tank_fail >= fail_thr,
        },
        "daikin_lwt": {
            "failed_24h": lwt_fail,
            "failing": lwt_fail >= fail_thr,
        },
    }


def actuation_issues(block: dict[str, Any]) -> list[str]:
    """Human-readable problems worth waking someone for; empty when healthy.

    Phrased so the message alone tells you what stopped and for how long — a
    page that only says "actuation unhealthy" costs a cockpit round-trip at the
    exact moment you want to act.
    """
    issues: list[str] = []

    fox = block.get("fox") or {}
    if fox.get("stale"):
        age = fox.get("age_hours")
        since = f"há {age:.0f}h" if isinstance(age, (int, float)) else "nunca"
        issues.append(
            f"Fox: nenhum upload de plano bem-sucedido {since} — o inversor está "
            "rodando uma schedule velha (bateria fora do plano)"
        )

    tank = block.get("daikin_tank") or {}
    if tank.get("stale"):
        age = tank.get("age_hours")
        since = f"há {age:.0f}h" if isinstance(age, (int, float)) else "nunca"
        issues.append(f"Daikin tanque: nenhuma ação executada {since} — reconciler parado")
    if tank.get("failing"):
        issues.append(f"Daikin tanque: {tank.get('failed_24h')} escritas rejeitadas em 24h")

    lwt = block.get("daikin_lwt") or {}
    if lwt.get("failing"):
        issues.append(f"Daikin LWT: {lwt.get('failed_24h')} escritas rejeitadas em 24h")

    return issues
