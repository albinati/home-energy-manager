#!/usr/bin/env bash
# Shared helpers for bin/*. (Sourced, not executed.)
#
# In Docker (or when HEM_IN_CONTAINER=1), use system Python — typically 3.11 in
# our image — not a bind-mounted .venv from the host (glibc / Python ABI mismatch).
# Override with HEM_PYTHON=/path/to/python if needed.

hem_python() {
  local root="$1"
  if [ -n "${HEM_PYTHON:-}" ]; then
    printf '%s\n' "${HEM_PYTHON}"
    return 0
  fi
  if [ -f /.dockerenv ] || [ -n "${HEM_IN_CONTAINER:-}" ]; then
    if command -v python3.11 >/dev/null 2>&1; then
      printf '%s\n' "$(command -v python3.11)"
      return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
      printf '%s\n' "$(command -v python3)"
      return 0
    fi
    echo "hem_python: no python3 in container PATH (use Python 3.11 image or set HEM_PYTHON)" >&2
    return 1
  fi
  local vpy="$root/.venv/bin/python"
  if [ -x "$vpy" ]; then
    printf '%s\n' "$vpy"
    return 0
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3.11)"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3)"
    return 0
  fi
  echo "hem_python: no Python found (create .venv, use Python 3.11+, or set HEM_PYTHON)" >&2
  return 1
}
