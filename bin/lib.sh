#!/usr/bin/env bash
# Shared helpers for bin/*. (Sourced, not executed.)
#
# Python selection priority:
#   1. $HEM_PYTHON (explicit override)
#   2. <project>/.venv/bin/python  (the venv created by `python3.12 -m venv .venv`)
#   3. system python3

hem_python() {
  local root="$1"
  if [ -n "${HEM_PYTHON:-}" ]; then
    printf '%s\n' "${HEM_PYTHON}"
    return 0
  fi
  local vpy="$root/.venv/bin/python"
  if [ -x "$vpy" ]; then
    printf '%s\n' "$vpy"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3)"
    return 0
  fi
  echo "hem_python: no Python found (create .venv or set HEM_PYTHON)" >&2
  return 1
}
