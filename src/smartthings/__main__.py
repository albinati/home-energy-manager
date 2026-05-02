"""Entry point for the one-shot OAuth enrollment container.

Run via the compose file:

    docker compose -f deploy/compose.smartthings-auth.yaml run --rm smartthings-auth

Equivalent to ``python -m src.smartthings`` — invokes ``auth.run_setup()``
which spawns the callback server, prints the Samsung consent URL, waits
for the redirect, exchanges the code for tokens, and persists them to
``config.SMARTTHINGS_TOKEN_FILE``.
"""
from __future__ import annotations

import sys

from .auth import run_setup


def main() -> int:
    result = run_setup()
    return 0 if result and result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
