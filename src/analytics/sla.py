"""SLA-style metrics from action_log and optimizer_log."""
from __future__ import annotations

from typing import Any

from .. import db


def compute_sla_metrics(limit: int = 500) -> dict[str, Any]:
    actions = db.get_action_logs(limit=limit)
    if not actions:
        return {
            "actions_executed_on_time_pct": None,
            "safe_default_restored_pct": None,
            "sample_size": 0,
        }
    ok = sum(1 for a in actions if a.get("result") == "success")
    restore = [a for a in actions if a.get("action") in ("apply_safe_defaults", "restore")]
    restore_ok = sum(1 for a in restore if a.get("result") == "success")
    opt = db.get_optimizer_logs(limit=50)
    opt_ok = sum(1 for o in opt if o.get("fox_schedule_uploaded")) if opt else 0
    return {
        "actions_executed_on_time_pct": round(100.0 * ok / len(actions), 2) if actions else None,
        "safe_default_restored_pct": round(100.0 * restore_ok / len(restore), 2) if restore else None,
        "optimizer_success_pct": round(100.0 * opt_ok / len(opt), 2) if opt else None,
        "sample_size": len(actions),
    }
