"""Uruchomienie usługi: `python -m bom_agent`.

Serwer MCP (stdio) dojdzie osobnym wejściem w #12 — tu startuje wyłącznie
warstwa HTTP dla PWA i `/health`.
"""

from __future__ import annotations

import os

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "bom_agent.web:create_app",
        factory=True,
        host=os.environ.get("BOM_HTTP_HOST", "0.0.0.0"),  # noqa: S104 — LAN, za NPM
        # 8091 — port zapisany dla bom-agent w docs/08-infrastruktura.md.
        port=int(os.environ.get("BOM_HTTP_PORT", "8091")),
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
