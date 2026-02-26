"""Alert notifier — stdout always; optional OpenClaw webhook (webchat, Telegram, etc.)."""
import json
import urllib.request
from datetime import datetime

from .config import config


def notify(message: str, urgent: bool = False) -> None:
    """Send a notification. Always prints to stdout; if ALERT_CHANNEL is set, sends to OpenClaw webhook."""
    prefix = "🚨" if urgent else "🏠"
    ts = datetime.now().strftime("%H:%M")
    full_msg = f"[{ts}] {prefix} energy-manager\n{message}"
    print(full_msg)

    if config.ALERT_CHANNEL:
        _send_via_openclaw(message=full_msg)


def _send_via_openclaw(message: str) -> bool:
    """Send via OpenClaw gateway (ALERT_OPENCLAW_URL). Uses ALERT_CHANNEL (e.g. webchat, telegram)."""
    try:
        payload: dict = {"message": message}
        if config.ALERT_CHANNEL:
            payload["channel"] = config.ALERT_CHANNEL
        req = urllib.request.Request(
            config.ALERT_OPENCLAW_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False
