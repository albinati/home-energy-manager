"""SmartThings integration — Personal Access Token (PAT) auth, REST API.

Phase 1 use case: detect when a Samsung washing machine is in remote-start
mode and schedule its cycle to fire during the cheapest energy window.

The client is a thin sync wrapper over ``urllib.request`` mirroring the
``src.daikin.client`` shape. The service module exposes a lazy singleton.
"""
from .client import SmartThingsClient, SmartThingsError

__all__ = ["SmartThingsClient", "SmartThingsError"]
