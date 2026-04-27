"""Google Family Calendar publisher.

Side feature: after every successful Octopus Agile fetch, classify each 30-min
slot into a 6-tier price bucket (day-relative thresholds with absolute floors
on the extremes), merge consecutive same-tier slots into windows, and publish
them as events on a shared Google Calendar so the household can plan heavy
appliance usage around cheap/peak windows.

Decoupled from the LP optimizer — this is pure rate visualization.
Idempotent: re-publish with the same prices is a no-op (unchanged events
are detected via stored hash and left alone).

One-shot OAuth bootstrap (installed-app flow):
    docker compose -f /srv/hem/compose.google-auth.yaml run --rm google-auth

Service-side use (called from scheduler.octopus_fetch on success):
    from src.google_calendar.publisher import publish_horizon
    publish_horizon()
"""
