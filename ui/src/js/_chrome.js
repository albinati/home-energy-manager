// Chrome bootstrap — populates the topbar mode badge from /api/v1/settings.
//
// Replaces the server-side Jinja2 render of `data-daikin` / `data-require-sim`
// attributes on the #modeBadge button. Called on every page load so the
// mode-switcher.js script (which reads those attrs) sees fresh values.
(function () {
  "use strict";

  function _markActiveTab() {
    // The active tab class was set server-side via Jinja's
    // {% if active_page == 'X' %}. Now we infer from window.location.
    const path = (window.location.pathname || "/").replace(/\/$/, "");
    document.querySelectorAll(".tab").forEach(a => {
      const href = (a.getAttribute("href") || "/").replace(/\/$/, "");
      if (href === path || (path === "" && href === "")) {
        a.classList.add("is-active");
      }
    });
  }

  async function _populateModeBadge() {
    const badge = document.getElementById("modeBadge");
    if (!badge) return;
    try {
      const r = await hemGetJson("/settings");
      // /api/v1/settings returns a flat dict; the mode-switcher partial
      // expects 'passive'|'active' and 'true'|'false' as data attrs.
      const dcm = (r && r.DAIKIN_CONTROL_MODE) || "";
      const rsi = !!(r && r.REQUIRE_SIMULATION_ID);
      badge.setAttribute("data-daikin", dcm || "passive");
      badge.setAttribute("data-require-sim", rsi ? "true" : "false");
      const value = badge.querySelector(".mode-value");
      if (value) value.textContent = dcm || "passive";
    } catch (e) {
      // Defensive: leave the placeholder attrs in place if /settings is
      // unreachable; mode_switcher.js handles missing data gracefully.
      console.warn("chrome: /settings fetch failed", e);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    _markActiveTab();
    _populateModeBadge();
  });
})();
