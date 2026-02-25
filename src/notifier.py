"""Alert notifier — sends to WhatsApp via a simple webhook or stdout fallback."""
import json
import urllib.request
import urllib.parse
from datetime import datetime

from .config import config


def notify(message: str, urgent: bool = False) -> None:
    """Send a notification. Prints to stdout always; also sends WhatsApp if configured."""
    prefix = "🚨" if urgent else "🏠"
    ts = datetime.now().strftime("%H:%M")
    full_msg = f"[{ts}] {prefix} energy-manager\n{message}"
    print(full_msg)

    number = config.ALERT_WHATSAPP_NUMBER
    if not number:
        return

    # Try OpenClaw gateway if available (local)
    _send_via_openclaw(full_msg, number)


def _send_via_openclaw(message: str, to: str) -> bool:
    """Send via local OpenClaw gateway WebSocket endpoint (if running)."""
    try:
        payload = json.dumps({
            "channel": "whatsapp",
            "to": to,
            "message": message,
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:18789/api/send",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False
