"""Registry completeness for the Settings UI (#879 PR 3).

Every runtime-settings key must be placed in exactly one group of
ui/src/components/settings/groups.ts (visible or Avancado), so a new
SettingSpec can never silently go missing from the UI.
"""
from __future__ import annotations

import re
from pathlib import Path

from src import runtime_settings as rts

GROUPS_TS = Path(__file__).resolve().parents[1] / "ui/src/components/settings/groups.ts"


def _group_keys() -> list[str]:
    text = GROUPS_TS.read_text(encoding="utf-8")
    start = text.index("export const VISIBLE_GROUPS")
    end = text.index("export const PLAIN_COPY")
    return re.findall(r'"([A-Z][A-Z0-9_]+)"', text[start:end])


def test_every_runtime_key_is_listed_in_a_ui_group():
    listed = set(_group_keys())
    missing = sorted(set(rts.SCHEMA) - listed)
    assert not missing, f"runtime keys not in any Settings UI group: {missing}"


def test_ui_groups_list_only_real_runtime_keys():
    unknown = sorted(set(_group_keys()) - set(rts.SCHEMA))
    assert not unknown, f"groups.ts lists keys absent from runtime_settings.SCHEMA: {unknown}"


def test_no_key_is_listed_twice():
    keys = _group_keys()
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, f"keys listed in more than one UI group: {dup}"
