"""SmartThings integration — OAuth 2.0 auth, REST API.

Phase 1 use case: detect when a Samsung washing machine is in remote-start
mode and schedule its cycle to fire during the cheapest energy window.

Auth model: OAuth Authorization Code flow (mirrors :mod:`src.daikin.auth`),
bootstrap via the one-shot ``deploy/compose.smartthings-auth.yaml`` container.
The client fetches a bearer token from :mod:`src.smartthings.auth` per
request and refreshes on HTTP 401.
"""
from .client import SmartThingsClient, SmartThingsError

__all__ = ["SmartThingsClient", "SmartThingsError"]
