"""Simulation-first action paradigm — diff payload + idempotency token store.

Every state-changing route in the API is paired with a ``/simulate`` route that
returns an :class:`ActionDiff` describing what would change, without writing.
The frontend cockpit (v10.1) walks the operator through preview → modal →
confirm before any real write happens.

Hard rule: simulate endpoints **must not** call cloud APIs (Onecta, FoxESS).
Diffs are computed exclusively from in-memory cache + SQLite reads. Tests in
``tests/api/test_simulate_writes.py`` enforce this by mocking the cloud
clients to raise on any call.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ActionDiff:
    """Structured diff returned by every ``/simulate`` endpoint.

    The frontend renders ``human_summary`` in the modal; ``before``/``after``
    populate a structured before/after panel; ``safety_flags`` raise a banner
    that requires explicit confirmation.
    """

    action: str  # e.g. "foxess.set_mode", "daikin.set_lwt_offset"
    before: dict[str, Any]
    after: dict[str, Any]
    affected_slots: list[str] = field(default_factory=list)
    cost_delta_pence: float | None = None
    soc_path_change: list[float] = field(default_factory=list)
    safety_flags: list[str] = field(default_factory=list)
    human_summary: str = ""
    simulation_id: str = ""
    expires_at_epoch: float = 0.0

    def to_response_dict(self) -> dict[str, Any]:
        return asdict(self)


class SimulationStore:
    """In-memory, thread-safe store of pending simulations with TTL expiry.

    Each simulate call registers an :class:`ActionDiff` and gets a UUID back.
    The paired real-write call passes the UUID via ``X-Simulation-Id`` header;
    we ``consume()`` it (one-shot — re-use is rejected). Expired entries are
    rejected with 410.

    No cloud API calls happen here. Just a Python dict with a lock.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        self._entries: dict[str, tuple[ActionDiff, float]] = {}

    def register(self, diff: ActionDiff) -> str:
        """Generate a simulation_id, stash the diff, return the id."""
        sid = uuid.uuid4().hex
        now = time.monotonic()
        diff.simulation_id = sid
        diff.expires_at_epoch = time.time() + self._ttl
        with self._lock:
            self._entries[sid] = (diff, now + self._ttl)
            self._gc_locked(now)
        return sid

    def get(self, simulation_id: str) -> ActionDiff | None:
        """Return diff if still valid; do NOT consume. Used by the new UI to
        re-render preview after a soft refresh."""
        with self._lock:
            entry = self._entries.get(simulation_id)
            if entry is None:
                return None
            diff, deadline = entry
            if time.monotonic() > deadline:
                self._entries.pop(simulation_id, None)
                return None
            return diff

    def consume(self, simulation_id: str) -> ActionDiff | None:
        """One-shot: return diff and remove it. Returns None if missing or expired.

        Caller distinguishes "missing" vs "expired" via ``get()`` if needed.
        """
        with self._lock:
            entry = self._entries.pop(simulation_id, None)
            if entry is None:
                return None
            diff, deadline = entry
            if time.monotonic() > deadline:
                return None
            return diff

    def _gc_locked(self, now: float) -> None:
        """Caller must hold ``self._lock``. Drops expired entries."""
        expired = [sid for sid, (_, deadline) in self._entries.items() if now > deadline]
        for sid in expired:
            self._entries.pop(sid, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# Module-level singleton — one store per process is enough.
_store = SimulationStore(ttl_seconds=300.0)


def get_store() -> SimulationStore:
    return _store


def reset_store_for_tests() -> None:
    """Clear the global store; only for use in pytest."""
    global _store
    _store = SimulationStore(ttl_seconds=300.0)
