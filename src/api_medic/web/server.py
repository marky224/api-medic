"""Uvicorn launcher.

Run with `python -m api_medic.web.server` (or `python -m api_medic.web`).
Phase 4 will replace this with the proper `api-medic serve` Typer command.
"""

from __future__ import annotations

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main() -> None:
    uvicorn.run(
        "api_medic.web.app:app",
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
