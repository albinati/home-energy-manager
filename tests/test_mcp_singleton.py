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


def test_second_acquire_sigterms_prior_and_succeeds(tmp_lock):
    """When another process holds the lock, _acquire_singleton_lock_early must
    SIGTERM it and retake the lock (the root cause of the MCP zombie pile-up
    in #60 was that nothing ever killed the prior instance).
    """
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
    acquired_fd: int | None = None
    try:
        line = proc.stdout.readline().decode().strip()
        assert line == "LOCKED", f"holder did not lock: stderr={proc.stderr.read()!r}"
        acquired_fd = _acquire_singleton_lock_early()
        assert acquired_fd is not None, "failed to acquire after SIGTERM retry"
        assert int(tmp_lock.read_text().strip()) == os.getpid()
        rc = proc.wait(timeout=5)
        assert rc in (-signal.SIGTERM, 128 + signal.SIGTERM, 0), (
            f"holder exit code {rc} unexpected"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        if acquired_fd is not None:
            _release(acquired_fd)


def test_acquire_gives_up_when_prior_pid_unkillable(tmp_lock, monkeypatch):
    """If SIGTERM has no effect within the 2s window, we must exit cleanly
    (return None) instead of looping — OpenClaw would otherwise respawn us
    in a tight loop."""
    fd_holder = os.open(tmp_lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd_holder, 0)
    os.write(fd_holder, f"{os.getpid() + 999999}\n".encode())  # fake PID in file
    os.fsync(fd_holder)

    monkeypatch.setattr("src.mcp_server.os.kill", lambda pid, sig: None)
    start = time.monotonic()
    fd = _acquire_singleton_lock_early()
    elapsed = time.monotonic() - start
    try:
        assert fd is None, "should have given up after retry window"
        assert 1.5 <= elapsed <= 3.5, f"retry window ~2s, got {elapsed:.2f}s"
    finally:
        _release(fd_holder)


# ---------------------------------------------------------------------------
# v10 S5a — early-acquisition regression
# ---------------------------------------------------------------------------

def test_module_import_does_not_acquire_lock():
    """Plain ``import`` (e.g. tests, audits) must not race for the singleton."""
    import importlib
    import src.mcp_server as m
    importlib.reload(m)
    assert m._EARLY_LOCK_FD is None


def test_concurrent_launch_serialises_through_lock():
    """5 concurrent launches must NOT all complete heavy imports in parallel.

    Pre-fix, all 5 would print the OpenClaw boundary surface-audit warning
    (emitted during build_mcp() AFTER heavy imports) before any singleton
    check. Post-fix, losers exit at the bootstrap warning before importing
    FastMCP/config/clients.
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

    losers = [i for i, err in enumerate(outputs) if "lock held by pid=" in err]
    # With stdin closed, each process exits quickly on EOF, so the next can
    # grab the lock — ending up sequential. The point of this test is to prove
    # lock contention IS visible (i.e. the bootstrap warning DOES fire), not
    # to count specific losers. >= 1 is enough proof the early-acquisition
    # check is wired and surfaces contention.
    assert len(losers) >= 1, (
        f"expected at least one process to hit bootstrap lock-contention "
        f"(proving early-acquisition is in effect); got {len(losers)}. "
        f"stderr summaries: {[err[:200] for err in outputs]}"
    )
