"""Safeguards for API actions: confirmation tokens, rate limiting, audit logging."""
import logging
import secrets
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from .models import ActionStatus, PendingAction

logger = logging.getLogger(__name__)

CONFIRMATION_EXPIRY_SECONDS = 300
RATE_LIMIT_COOLDOWN_SECONDS = 5

_pending_actions: dict[str, PendingAction] = {}
_last_action_time: dict[str, datetime] = {}
_lock = Lock()


def generate_action_id() -> str:
    return secrets.token_urlsafe(16)


def create_pending_action(
    action_type: str,
    description: str,
    parameters: dict[str, Any],
) -> PendingAction:
    """Create a pending action that requires confirmation."""
    action_id = generate_action_id()
    expires_at = datetime.now() + timedelta(seconds=CONFIRMATION_EXPIRY_SECONDS)
    
    action = PendingAction(
        action_id=action_id,
        action_type=action_type,
        description=description,
        parameters=parameters,
        expires_at=expires_at,
        status=ActionStatus.PENDING,
    )
    
    with _lock:
        _pending_actions[action_id] = action
    
    logger.info(f"Created pending action {action_id}: {action_type} - {description}")
    return action


def get_pending_action(action_id: str) -> PendingAction | None:
    """Get a pending action by ID."""
    with _lock:
        action = _pending_actions.get(action_id)
        if action is None:
            return None
        if datetime.now() > action.expires_at:
            action.status = ActionStatus.EXPIRED
            return action
        return action


def confirm_action(action_id: str) -> PendingAction | None:
    """Confirm a pending action, returning it if valid."""
    with _lock:
        action = _pending_actions.get(action_id)
        if action is None:
            return None
        
        if datetime.now() > action.expires_at:
            action.status = ActionStatus.EXPIRED
            return action
        
        if action.status != ActionStatus.PENDING:
            return action
        
        action.status = ActionStatus.CONFIRMED
        logger.info(f"Confirmed action {action_id}: {action.action_type}")
        return action


def cancel_action(action_id: str) -> PendingAction | None:
    """Cancel a pending action."""
    with _lock:
        action = _pending_actions.get(action_id)
        if action is None:
            return None
        
        action.status = ActionStatus.CANCELLED
        logger.info(f"Cancelled action {action_id}: {action.action_type}")
        return action


def mark_executed(action_id: str) -> None:
    """Mark an action as executed."""
    with _lock:
        action = _pending_actions.get(action_id)
        if action:
            action.status = ActionStatus.EXECUTED
            logger.info(f"Executed action {action_id}: {action.action_type}")


def check_rate_limit(action_type: str) -> tuple[bool, float | None]:
    """
    Check if an action is rate limited.
    Returns (allowed, seconds_until_allowed).
    """
    with _lock:
        last_time = _last_action_time.get(action_type)
        if last_time is None:
            return True, None
        
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < RATE_LIMIT_COOLDOWN_SECONDS:
            return False, RATE_LIMIT_COOLDOWN_SECONDS - elapsed
        
        return True, None


def record_action_time(action_type: str) -> None:
    """Record the time of an action for rate limiting."""
    with _lock:
        _last_action_time[action_type] = datetime.now()


def cleanup_expired_actions() -> int:
    """Remove expired actions from memory. Returns count of removed actions."""
    now = datetime.now()
    removed = 0
    with _lock:
        expired_ids = [
            aid for aid, action in _pending_actions.items()
            if now > action.expires_at + timedelta(minutes=5)
        ]
        for aid in expired_ids:
            del _pending_actions[aid]
            removed += 1
    return removed


def audit_log(
    action_type: str,
    parameters: dict[str, Any],
    source: str,
    success: bool,
    message: str,
) -> None:
    """Log an action for audit purposes."""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "action": action_type,
        "parameters": parameters,
        "source": source,
        "success": success,
        "message": message,
    }
    if success:
        logger.info(f"AUDIT: {log_entry}")
    else:
        logger.warning(f"AUDIT FAILED: {log_entry}")


ACTIONS_REQUIRING_CONFIRMATION = {
    "daikin.power",
    "daikin.tank_power",
    "foxess.mode",
}


def requires_confirmation(action_type: str) -> bool:
    """Check if an action type requires confirmation."""
    return action_type in ACTIONS_REQUIRING_CONFIRMATION
