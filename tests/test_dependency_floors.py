"""Dependency guards for imports the whole app is built on.

These exist because an unpinned floor took CI down for 11 days without anyone
noticing, and would have taken production down on the next deploy.

`mcp>=1.0.0` (no cap) let CI and the Docker image builder pull mcp 2.0.0, which
removed `mcp.server.fastmcp`. Every API test module imports it transitively, so
the breakage surfaced as ~40 collection errors naming individual test files —
noise that reads like a test problem, not a dependency problem. Main went red on
2026-07-28 and stayed red; production survived only because its image tag was
pinned to the last green build (2026-07-24). The image published on
2026-08-08 crashed on import and was caught by a pre-restart guard, not by CI.

A single named assertion is worth more than 40 anonymous ones.
"""

import importlib
import importlib.metadata

import pytest


def test_fastmcp_import_path_exists():
    """`build_mcp()` and every API test module depend on this exact path."""
    try:
        importlib.import_module("mcp.server.fastmcp")
    except ModuleNotFoundError as e:  # pragma: no cover — the failure IS the point
        pytest.fail(
            f"mcp.server.fastmcp is gone (installed mcp "
            f"{importlib.metadata.version('mcp')}): {e}. mcp 2.0 removed this "
            "path; requirements.txt caps below 2.0 for that reason. If the cap "
            "was raised deliberately, src/mcp_server.py has to migrate to the "
            "2.x API first."
        )


def test_mcp_major_version_is_capped():
    """Fail loudly on the version, not on a downstream import 40 files later."""
    major = int(importlib.metadata.version("mcp").split(".")[0])
    assert major < 2, (
        "mcp 2.x removed mcp.server.fastmcp — src/mcp_server.py must migrate "
        "before the cap in requirements.txt is raised"
    )
