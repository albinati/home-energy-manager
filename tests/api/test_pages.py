"""PR-B: cockpit page smoke tests.

Each new page must render HTML with the expected nav + skill-relevant titles.
Static assets (JS modules) must serve through the StaticFiles mount.
The new /api/v1/load/breakdown endpoint must return a stable shape.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

PAGES = [
    ("/", "cockpit", "Cockpit"),
    ("/insights", "insights", "Insights"),
    ("/plan", "plan", "Plan"),
    ("/settings", "settings", "Settings"),
    ("/legacy", "legacy", "Home Energy Manager"),
]


@pytest.mark.parametrize("url,active_tab,expected_in_html", PAGES)
def test_page_renders(client, url, active_tab, expected_in_html):
    r = client.get(url)
    assert r.status_code == 200, f"{url}: HTTP {r.status_code}"
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    assert expected_in_html in body, f"{url}: missing {expected_in_html!r}"


def test_nav_links_present_on_v10_pages(client):
    """All new pages share the _layout.html nav. Confirm every link is present."""
    for url in ("/", "/insights", "/plan", "/settings"):
        body = client.get(url).text
        for tab_url, tab_label in [("/", "Cockpit"), ("/insights", "Insights"),
                                    ("/plan", "Plan"), ("/settings", "Settings")]:
            assert f'href="{tab_url}"' in body, f"{url}: missing nav link to {tab_url}"
            assert tab_label in body, f"{url}: missing nav label {tab_label}"


def test_modal_partial_included(client):
    """Every v10 page must include the modal partial (the simulate-confirm UI)."""
    for url in ("/", "/insights", "/plan", "/settings"):
        body = client.get(url).text
        assert 'id="modalBackdrop"' in body, f"{url}: modal not included"


def test_mode_badge_rendered_on_every_v10_page(client):
    """v10.2: every page renders the topbar mode badge with data-mode attr."""
    for url in ("/", "/insights", "/plan", "/settings"):
        body = client.get(url).text
        assert 'class="mode-badge"' in body, f"{url}: mode badge missing"
        assert 'data-mode=' in body, f"{url}: data-mode attribute missing"
        assert 'id="modeBadge"' in body, f"{url}: badge id missing (click handler won't bind)"


def test_mode_switcher_partial_included(client):
    """v10.2: the mode-switcher dialog partial must be on every page."""
    for url in ("/", "/insights", "/plan", "/settings"):
        body = client.get(url).text
        assert 'id="modeSwitcherBackdrop"' in body, f"{url}: mode switcher not included"


def test_settings_no_longer_renders_legacy_mode_blocks(client):
    """v10.2: mode controls moved to badge — settings page must not double up."""
    body = client.get("/settings").text
    for legacy_id in ('settingDaikinControlMode', 'settingRequireSim', 'settingOperationMode'):
        assert legacy_id not in body, f"settings still has legacy {legacy_id} block"
    assert 'mode badge' in body.lower(), "settings should redirect users to the mode badge"


def test_mode_switcher_js_served(client):
    r = client.get("/static/js/mode_switcher.js")
    assert r.status_code == 200
    assert 'wrapAction' in r.text
    assert 'modeSwitcherBackdrop' in r.text


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

STATIC_ASSETS = [
    "/static/css/cockpit.css",
    "/static/js/modal.js",
    "/static/js/quota.js",
    "/static/js/cockpit.js",
    "/static/js/insights.js",
    "/static/js/plan.js",
    "/static/js/settings.js",
]


@pytest.mark.parametrize("path", STATIC_ASSETS)
def test_static_asset_served(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path}: HTTP {r.status_code}"
    assert len(r.content) > 100, f"{path}: too small ({len(r.content)} bytes)"


def test_modal_js_exposes_wrapaction(client):
    """The wrapAction helper is the heart of simulate-first; it must be in modal.js."""
    body = client.get("/static/js/modal.js").text
    assert "wrapAction" in body
    assert "X-Simulation-Id" in body
    assert "simulate" in body.lower()


def test_cockpit_default_no_auto_refresh(client):
    """Daikin auto-refresh must default to OFF (operator's explicit choice)."""
    body = client.get("/static/js/cockpit.js").text
    # No setInterval anywhere — initial loads + manual refresh only
    assert "setInterval" not in body, "cockpit.js must not auto-poll"


# ---------------------------------------------------------------------------
# /api/v1/load/breakdown
# ---------------------------------------------------------------------------

def test_load_breakdown_shape(client):
    r = client.get("/api/v1/load/breakdown")
    assert r.status_code == 200
    d = r.json()
    for key in ("house_total_kw", "daikin_estimate_kw", "daikin_outdoor_c",
                "daikin_source", "residual_kw", "from_cache"):
        assert key in d, f"missing key {key!r} in breakdown payload"
    assert d["from_cache"] is True


def test_load_breakdown_does_not_force_refresh(client):
    """The breakdown endpoint must never trigger Daikin/Fox cloud refresh."""
    from unittest.mock import patch
    with patch("src.daikin.service.force_refresh_devices",
               side_effect=AssertionError("breakdown must not force-refresh Daikin")):
        r = client.get("/api/v1/load/breakdown")
        assert r.status_code == 200
