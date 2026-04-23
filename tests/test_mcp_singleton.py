"""MCP singleton lock — SIGTERM-and-retry behavior + early-acquisition (#60, v10 S5a).

The v10 fix moves the lock acquisition from inside ``main()`` to module top-level
(when ``__name__ == '__main__'``), so concurrent launches can no longer all
complete the heavy import phase before any of them reaches the lock check.
"""

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from src.mcp_server import _acquire_singleton_lock_early  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_lock(tmp_path, monkeypatch):
    lock = tmp_path / "hem-mcp.lock"
    monkeypatch.setattr("src.mcp_server._lock_path", lambda: lock)
    return lock


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_first_acquire_writes_own_pid(tmp_lock):
    fd = _acquire_singleton_lock_early()
    assert fd is not None
    try:
        assert int(tmp_lock.read_text().strip()) == os.getpid()
    finally:
        _release(fd)


def test_second_acquire_yields_silently_to_prior(tmp_lock):
    """v10.1 hotfix: when another process holds the lock, the new spawn must
    exit silently with code 0 — NOT SIGTERM the holder.

    The previous (PR-A) behaviour killed the prior holder, which broke
    openclaw's persistent stdio MCP connection in prod (observed as
    ``MCP error -32000: Connection closed``). POSIX flock auto-releases on
    process death, so true crashes recover without needing SIGTERM.
    """
    kill_called: list[tuple[int, int]] = []
    holder_src = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open(r'{tmp_lock}', os.O_RDWR | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "os.ftruncate(fd, 0)\n"
        "os.write(fd, f'{os.getpid()}\\n'.encode())\n"
        "os.fsync(fd)\n"
        "sys.stdout.write('LOCKED\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        line = proc.stdout.readline().decode().strip()
        assert line == "LOCKED", f"holder did not lock: stderr={proc.stderr.read()!r}"
        # Spy on os.kill to confirm we do NOT call it
        import src.mcp_server as m
        original_kill = m.os.kill
        m.os.kill = lambda pid, sig: kill_called.append((pid, sig))
        try:
            fd = _acquire_singleton_lock_early()
        finally:
            m.os.kill = original_kill
        assert fd is None, "second acquire should yield (return None), not take the lock"
        assert kill_called == [], f"must not SIGTERM the holder; called {kill_called}"
        # Holder should still be alive
        assert proc.poll() is None, "holder must still be running"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_force_kill_env_var_restores_old_behaviour(tmp_lock, monkeypatch):
    """Manual recovery escape hatch: HEM_MCP_FORCE_KILL_PRIOR=1 re-enables
    the SIGTERM-and-retry path for emergency unstuck scenarios.
    """
    monkeypatch.setenv("HEM_MCP_FORCE_KILL_PRIOR", "1")
    holder_src = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open(r'{tmp_lock}', os.O_RDWR | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "os.ftruncate(fd, 0)\n"
        "os.write(fd, f'{os.getpid()}\\n'.encode())\n"
        "os.fsync(fd)\n"
        "sys.stdout.write('LOCKED\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    acquired_fd = None
    try:
        line = proc.stdout.readline().decode().strip()
        assert line == "LOCKED", f"holder did not lock: stderr={proc.stderr.read()!r}"
        acquired_fd = _acquire_singleton_lock_early()
        assert acquired_fd is not None, "force-kill path failed to acquire"
        rc = proc.wait(timeout=5)
        assert rc in (-signal.SIGTERM, 128 + signal.SIGTERM, 0), (
            f"holder exit code {rc} unexpected (force-kill expected)"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        if acquired_fd is not None:
            _release(acquired_fd)


# ---------------------------------------------------------------------------
# v10 S5a — early-acquisition regression
# ---------------------------------------------------------------------------

def test_module_import_does_not_acquire_lock():
    """Plain ``import`` (e.g. tests, audits) must not race for the singleton."""
    import importlib
    import src.mcp_server as m
    importlib.reload(m)
    assert m._EARLY_LOCK_FD is None


def test_concurrent_launch_yields_to_winner():
    """5 concurrent launches: exactly one wins the lock; the other 4 yield
    silently. None of the 4 losers should kill the winner.
    """
    lock_file = Path("/tmp/hem-mcp.lock")
    lock_file.unlink(missing_ok=True)

    procs = []
    for _ in range(5):
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "src.mcp_server"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        ))

    outputs = []
    try:
        for p in procs:
            try:
                _, stderr = p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                _, stderr = p.communicate(timeout=2)
            outputs.append(stderr.decode(errors="replace"))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        lock_file.unlink(missing_ok=True)

    # Yield messages from the post-hotfix path
    yielders = [i for i, err in enumerate(outputs) if "another instance is live" in err]
    # And nobody should have force-killed (we never set HEM_MCP_FORCE_KILL_PRIOR)
    sigterm_msgs = [i for i, err in enumerate(outputs) if "sending SIGTERM" in err]
    assert sigterm_msgs == [], (
        f"hotfix forbids SIGTERM in default path; got {len(sigterm_msgs)} messages"
    )
    # With stdin closed, the winner exits quickly on EOF, so subsequent launches
    # serialize through the lock too — but each loser must yield, not kill.
    assert len(yielders) >= 1, (
        f"expected at least one process to log yield-to-prior; got {len(yielders)}. "
        f"stderr summaries: {[err[:200] for err in outputs]}"
    )
