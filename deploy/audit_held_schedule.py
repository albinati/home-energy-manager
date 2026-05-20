"""Daily prod audit shim — calls ``audit_report.build_audit_report`` + posts
the rendered markdown to Telegram when there's something to report.

Lives on the prod host at ``/srv/hem/data/audit_held_schedule.py`` and is
invoked daily at 07:30 UTC by ``hem-audit-held-schedule.timer``. Runs
inside the hem container via ``docker exec``.

The previous 415-line in-data script was lifted into the repo at
``src/analytics/audit_report.py`` (Story A1, Epic 13a). This shim keeps
the on-host behaviour identical (silent on quiet days, single Telegram
push when there's news) while making the analytics testable and reusable
by MCP tools / briefs.

Deploy: ``scp deploy/audit_held_schedule.py
root@<prod>:/srv/hem/data/audit_held_schedule.py``. No restart needed —
the cron timer re-reads the file on every fire.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from src.analytics.audit_report import (  # noqa: E402
    build_audit_report,
    render_audit_markdown,
)
from src.telegram_transport import send_message  # noqa: E402


def main() -> int:
    report = build_audit_report(window_hours=24)
    body = render_audit_markdown(report)
    if body is None:
        print("Nothing notable to report — Telegram silent.")
        return 0
    sent = send_message(body, convert_markdown=False)
    print(f"Audit posted to Telegram (ok={sent}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
