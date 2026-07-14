"""The DHW package may not read the energy counter. Ever.

This is a grep test, and it is the cheapest test in the repo. It exists because the
failure it guards against is not hypothetical — it happened, it produced two
confident and completely false findings, and the pull that caused it will come back.

``daikin_consumption_2hourly.kwh_dhw`` is not a measurement of anything:

* half its rows (``source='onecta_cache'``) are Daikin's own counter, **quantised to
  whole kWh**. A 0.6 kWh post-shower reheat reads **0.0**. The code already knew:
  see ``src/daikin/service.py`` and #425.
* the other half (``source='telemetry_integral'``) is **synthesised by this codebase**
  as ``kwh = (C·ΔT_tank + loss) / cop_assumed``. It is the tank's own temperature
  series, divided by a constant.

So any energy balance drawn against tank temperature is circular, and any COP fitted
from it returns the assumption that was divided by. That is exactly what happened:
a "measured DHW COP of 2.6" that was the echo of a hard-coded 2.6, and a draw profile
that placed this household's showers in the morning when they are at 20:00.

The rule the package lives by: **the thermometer, the weather, and Daikin's databook.
Nothing else.** A real COP measurement needs a CT clamp on the heat pump, not a
cleverer fit — and if someone adds one, they should delete this test deliberately,
not route around it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_FORBIDDEN = {
    "kwh_dhw": "the DHW energy counter — quantised to whole kWh AND partly synthesised "
               "from the tank temperature itself (#719). Circular by construction.",
    "consumption_2hourly": "the table that counter lives in.",
    "dhw_error_log": "built by comparing forecasts against that same counter.",
    "dhw_bucket_bias": "a shape corrector calibrated against that same counter.",
    "COP_DHW_PENALTY": "the legacy fudge factor — a subtraction in one file and a "
                       "multiplier in another, and wrong both ways (#717). The COP "
                       "comes from the certified EN 16147 curve in dhw.model.",
}


def _package_sources() -> list[Path]:
    pkg = Path(__file__).resolve().parents[2] / "src" / "dhw"
    files = sorted(pkg.glob("*.py"))
    assert files, "src/dhw is empty — this test would pass vacuously"
    return files


def _executable_text(source: str) -> str:
    """Everything in the module EXCEPT its prose.

    Docstrings and comments are excluded deliberately: the package is required to
    explain at length why it refuses to touch the counter, and a naive grep would
    flag exactly the explanations that make the rule stick. Everything that actually
    runs — identifiers, attributes, and any string that could be a query — is still
    checked, which is where a real violation would live.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.alias):
            parts.append(node.name)
            if node.asname:
                parts.append(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts.append(node.module)
    return "\n".join(parts)


@pytest.mark.parametrize("path", _package_sources(), ids=lambda p: p.name)
def test_dhw_package_never_touches_the_broken_instrument(path: Path):
    code = _executable_text(path.read_text())
    for needle, why in _FORBIDDEN.items():
        assert needle not in code, (
            f"{path.name} references {needle!r} in CODE (not prose).\n\n{why}\n\n"
            "If you are adding a real measurement (a CT clamp on the heat pump), "
            "delete this rule deliberately and say so. If you are 'just using it as "
            "a rough signal' — that is precisely how the last two false findings got "
            "in. See the module docstring."
        )


def test_the_guard_would_actually_catch_a_violation():
    """A rule nobody can fail is a rule nobody is following. Prove the guard bites."""
    sneaky = 'def f():\n    """We must never read kwh_dhw."""\n    return db.get(kwh_dhw)\n'
    code = _executable_text(sneaky)
    assert "kwh_dhw" in code  # the call is caught...

    prose_only = 'def f():\n    """We must never read kwh_dhw."""\n    return 1\n'
    assert "kwh_dhw" not in _executable_text(prose_only)  # ...the explanation is not
