"""Consent workflow for optimization plans.

Plans must be proposed to the user, reviewed, and explicitly approved before
the executor is allowed to dispatch commands to hardware. This extends the
safeguards.py confirmation pattern used for individual actions.

Manual flow (default, PLAN_AUTO_APPROVE=false):
  1. propose_plan(plan)  -> PendingPlan (with summary, token, expiry)
  2. User reviews summary via OpenClaw tool or API
  3. approve_plan(plan_id) -> marks APPROVED; executor will act on it
     OR reject_plan(plan_id) -> discards it
  4. Approved plan is consumed by executor each 30-min tick until it expires
     or a new plan is proposed.

Auto-approve flow (PLAN_AUTO_APPROVE=true):
  1. propose_plan(plan) immediately calls approve_plan internally
  2. Returns the plan already in APPROVED state
  3. Notification is sent to OpenClaw / stdout so the user is still informed
  4. User can still call reject_plan at any time to stop the current plan
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

from ..config import config
from .models import PendingPlan, PlanConsentStatus, SolverPlan

logger = logging.getLogger(__name__)

_lock = Lock()
_pending_plan: Optional[PendingPlan] = None
_approved_plan: Optional[PendingPlan] = None


def _make_plan_summary(plan: SolverPlan) -> str:
    """Build a human-readable summary of a solver plan for OpenClaw / API response."""
    cheap_slots = [s for s in plan.slots if s.slot_kind.value == "cheap"]
    peak_slots = [s for s in plan.slots if s.slot_kind.value == "peak"]
    force_charge_slots = [s for s in plan.slots if s.fox_mode_hint.value == "Force charge"]
    force_discharge_slots = [s for s in plan.slots if s.fox_mode_hint.value == "Force discharge"]

    cheap_times = []
    for s in cheap_slots[:3]:
        cheap_times.append(
            f"{s.valid_from.strftime('%H:%M')} ({s.import_price_pence:.1f}p)"
        )
    peak_times = []
    for s in peak_slots[:3]:
        peak_times.append(
            f"{s.valid_from.strftime('%H:%M')} ({s.import_price_pence:.1f}p)"
        )

    lines = [
        f"Preset: {plan.preset.value}  |  Tariff: {plan.tariff_code or 'unknown'}",
        f"48-slot plan — cheap slots: {len(cheap_slots)}, peak slots: {len(peak_slots)}",
        f"Mean import price: {plan.target_mean_price_pence:.2f}p/kWh",
    ]
    if config.TARGET_PRICE_PENCE > 0:
        gap = plan.target_mean_price_pence - config.TARGET_PRICE_PENCE
        direction = "above" if gap > 0 else "below"
        lines.append(
            f"Target price: {config.TARGET_PRICE_PENCE:.1f}p  "
            f"({abs(gap):.1f}p {direction} target)"
        )
    if cheap_times:
        lines.append(f"Cheapest windows: {', '.join(cheap_times)}")
    if peak_times:
        lines.append(f"Peak windows: {', '.join(peak_times)}")
    if force_charge_slots:
        fc = force_charge_slots[0]
        lines.append(
            f"Force charge from {fc.valid_from.strftime('%H:%M')} "
            f"({len(force_charge_slots)} slots)"
        )
    if force_discharge_slots:
        fd = force_discharge_slots[0]
        lines.append(
            f"Force discharge from {fd.valid_from.strftime('%H:%M')} "
            f"({len(force_discharge_slots)} slots)"
        )
    lines.append(
        "Approve with approve_optimization_plan or "
        "POST /api/v1/optimization/approve to activate."
    )
    return "\n".join(lines)


def propose_plan(plan: SolverPlan) -> PendingPlan:
    """Store a solver plan as pending consent and return it with a summary.

    Any existing pending (unapproved) plan is replaced. An already-approved
    plan is not replaced — call reject_plan first if you want to reset.

    When PLAN_AUTO_APPROVE=true the plan is immediately approved without waiting
    for explicit user consent. A notification is always sent so the user is aware.
    """
    global _pending_plan, _approved_plan
    plan_id = secrets.token_urlsafe(12)
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=config.PLAN_CONSENT_EXPIRY_SECONDS)
    summary = _make_plan_summary(plan)

    if config.PLAN_AUTO_APPROVE:
        # Skip the pending queue — go straight to approved
        pending = PendingPlan(
            plan_id=plan_id,
            plan=plan,
            proposed_at=now,
            expires_at=expiry,
            status=PlanConsentStatus.APPROVED,
            summary=summary,
            approved_at=now,
        )
        with _lock:
            _pending_plan = None
            _approved_plan = pending
        logger.info(
            "Optimization plan %s AUTO-APPROVED (PLAN_AUTO_APPROVE=true)", plan_id
        )
        # Notify so the user is always informed, even in auto-approve mode
        try:
            from ..notifier import notify
            notify(
                f"[AUTO-APPROVE] New optimization plan activated ({plan_id[:8]}).\n"
                f"{summary}\n"
                f"Call reject_optimization_plan('{plan_id}') to stop it."
            )
        except Exception:
            pass
    else:
        pending = PendingPlan(
            plan_id=plan_id,
            plan=plan,
            proposed_at=now,
            expires_at=expiry,
            status=PlanConsentStatus.PENDING,
            summary=summary,
        )
        with _lock:
            _pending_plan = pending
        logger.info(
            "Optimization plan proposed (id=%s, expires=%s)", plan_id, expiry.isoformat()
        )

    return pending


def get_pending_plan() -> Optional[PendingPlan]:
    """Return the current pending plan, or None if there isn't one / it has expired."""
    with _lock:
        p = _pending_plan
    if p is None:
        return None
    if datetime.now(timezone.utc) > p.expires_at and p.status == PlanConsentStatus.PENDING:
        with _lock:
            if _pending_plan and _pending_plan.plan_id == p.plan_id:
                _pending_plan.status = PlanConsentStatus.EXPIRED
        logger.info("Pending plan %s expired", p.plan_id)
        return _pending_plan  # return with EXPIRED status so callers can report it
    return p


def get_approved_plan() -> Optional[PendingPlan]:
    """Return the currently approved plan (used by executor each tick)."""
    with _lock:
        p = _approved_plan
    if p is None:
        return None
    if datetime.now(timezone.utc) > p.expires_at:
        logger.info("Approved plan %s expired; executor will use baseline", p.plan_id)
        with _lock:
            if _approved_plan and _approved_plan.plan_id == p.plan_id:
                _approved_plan.status = PlanConsentStatus.EXPIRED
        return None
    return p


def approve_plan(plan_id: str) -> Optional[PendingPlan]:
    """Approve a pending plan by its ID. Returns the plan or None if not found/expired."""
    global _pending_plan, _approved_plan
    with _lock:
        p = _pending_plan
        if p is None or p.plan_id != plan_id:
            return None
        if datetime.now(timezone.utc) > p.expires_at:
            p.status = PlanConsentStatus.EXPIRED
            return p
        if p.status != PlanConsentStatus.PENDING:
            return p
        p.status = PlanConsentStatus.APPROVED
        p.approved_at = datetime.now(timezone.utc)
        _approved_plan = p
        _pending_plan = None
    logger.info("Optimization plan %s APPROVED by user", plan_id)
    return p


def reject_plan(plan_id: str) -> Optional[PendingPlan]:
    """Reject a pending plan. Clears it so a new one can be proposed."""
    global _pending_plan, _approved_plan
    with _lock:
        # Try pending first, then approved
        p = _pending_plan if (_pending_plan and _pending_plan.plan_id == plan_id) else None
        if p is None:
            p = _approved_plan if (_approved_plan and _approved_plan.plan_id == plan_id) else None
            if p:
                _approved_plan = None
        if p is None:
            return None
        p.status = PlanConsentStatus.REJECTED
        p.rejected_at = datetime.now(timezone.utc)
        if _pending_plan and _pending_plan.plan_id == plan_id:
            _pending_plan = None
    logger.info("Optimization plan %s REJECTED by user", plan_id)
    return p


def clear_approved_plan() -> None:
    """Clear the approved plan (e.g. on rollback or mode switch to simulation)."""
    global _approved_plan
    with _lock:
        _approved_plan = None
    logger.info("Approved plan cleared")


def consent_status_dict() -> dict:
    """Return a JSON-friendly summary of consent state for dashboards and OpenClaw."""
    pending = get_pending_plan()
    approved = get_approved_plan()
    return {
        "auto_approve": config.PLAN_AUTO_APPROVE,
        "pending_plan_id": pending.plan_id if pending else None,
        "pending_plan_status": pending.status.value if pending else None,
        "pending_plan_expires_at": pending.expires_at.isoformat() if pending else None,
        "pending_plan_summary": pending.summary if pending else None,
        "approved_plan_id": approved.plan_id if approved else None,
        "approved_plan_approved_at": approved.approved_at.isoformat() if approved and approved.approved_at else None,
        "approved_plan_expires_at": approved.expires_at.isoformat() if approved else None,
    }


def set_auto_approve(enabled: bool) -> None:
    """Enable or disable auto-approve at runtime (without restarting the process)."""
    config.PLAN_AUTO_APPROVE = enabled
    logger.info("PLAN_AUTO_APPROVE set to %s", enabled)
