"""CLI entrypoints for the Google Calendar feature.

    python -m src.google_calendar           # one-shot OAuth bootstrap (default)
    python -m src.google_calendar publish   # publish today + tomorrow once and exit

The OAuth bootstrap is invoked by ``deploy/compose.google-auth.yaml`` to walk
the operator through the installed-app flow. After the tokens land at
``GOOGLE_CALENDAR_TOKEN_FILE`` the long-running ``hem`` service refreshes
the access token automatically thereafter (separate APScheduler job at
``GOOGLE_CALENDAR_PUBLISH_INTERVAL_MINUTES`` cadence).
"""
import json
import logging
import sys

from .auth import GoogleCalendarAuthError, run_oauth_flow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> int:
    sub = sys.argv[1] if len(sys.argv) > 1 else "oauth"

    if sub == "publish":
        from .publisher import publish_horizon
        try:
            result = publish_horizon()
        except GoogleCalendarAuthError as e:
            print(f"\nERROR: {e}\n", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    if sub == "cleanup-legacy":
        from .publisher import cleanup_legacy_events
        try:
            result = cleanup_legacy_events()
        except GoogleCalendarAuthError as e:
            print(f"\nERROR: {e}\n", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    if sub in ("oauth", "auth"):
        try:
            run_oauth_flow()
        except GoogleCalendarAuthError as e:
            print(f"\nERROR: {e}\n", file=sys.stderr)
            return 1
        print("\nOAuth complete. The hem service will publish rate windows on the next scheduler tick.")
        return 0

    print(
        f"Unknown subcommand: {sub}\n"
        "Usage: python -m src.google_calendar [oauth|publish|cleanup-legacy]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
