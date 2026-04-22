"""MCP singleton lock — SIGTERM-and-retry behavior (#60)."""

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time

import pytest

pytest.importorskip("mcp")

from src.mcp_server import _acquire_singleton_lock  # noqa: E402


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
    log = logging.getLogger("test")
    fd = _acquire_singleton_lock(log)
    assert fd is not None
    try:
        assert int(tmp_lock.read_text().strip()) == os.getpid()
    finally:
        _release(fd)


def test_second_acquire_sigterms_prior_and_succeeds(tmp_lock):
    """When another process holds the lock, _acquire_singleton_lock must
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
        log = logging.getLogger("test")
        acquired_fd = _acquire_singleton_lock(log)
        assert acquired_fd is not None, "failed to acquire after SIGTERM retry"
        assert int(tmp_lock.read_text().strip()) == os.getpid()
        rc = proc.wait(timeout=5)
        # SIGTERM → -15 on POSIX. Accept either -signal or conventional 128+signal.
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
    # Hold the lock in this same process via another fd; our own PID cannot
    # be SIGTERM'd out from under us without actually terminating the test.
    # Monkey-patch os.kill to a no-op so the retry loop runs to the end.
    fd_holder = os.open(tmp_lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd_holder, 0)
    os.write(fd_holder, f"{os.getpid() + 999999}\n".encode())  # fake PID in file
    os.fsync(fd_holder)

    monkeypatch.setattr("src.mcp_server.os.kill", lambda pid, sig: None)
    log = logging.getLogger("test")
    start = time.monotonic()
    fd = _acquire_singleton_lock(log)
    elapsed = time.monotonic() - start
    try:
        assert fd is None, "should have given up after retry window"
        assert 1.5 <= elapsed <= 3.5, f"retry window ~2s, got {elapsed:.2f}s"
    finally:
        _release(fd_holder)
