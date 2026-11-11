#!/usr/bin/env python3
"""Smoke serwera MCP uruchomionego W KONTENERZE na hoście usług.

Kolejka intake i zdjęcia leżą w wolumenie kontenera, więc serwer MCP musi
działać tam, gdzie te dane są. Transport pozostaje stdio — tunelem jest
`docker exec -i` (na innej maszynie: `ssh` + `docker exec -i`).

    uv run python scripts/smoke_intake_mcp.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(
    command="docker",
    args=[
        "exec",
        "-i",
        "bom-agent",
        "python",
        "-m",
        "bom_agent.mcp_server",
    ],
    env=dict(os.environ),
)


async def main() -> int:
    async with stdio_client(SERVER) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            info = await session.initialize()
            print(f"serwer     : {info.server_info.name} {info.server_info.version}")

            tools = await session.list_tools()
            print(f"narzędzia  : {[tool.name for tool in tools.tools]}")

            result = await session.call_tool("intake_pending", {"limit": 3})
            for block in result.content:
                kind = getattr(block, "type", "?")
                if kind == "text":
                    print(block.text)
                else:
                    data = getattr(block, "data", "") or ""
                    print(f"[{kind}] {len(data) * 3 // 4 // 1024} kB obrazu")

            # Próba na sucho: bez `confirm` narzędzie ma pokazać plan
            # i NIE ruszyć magazynu.
            print("\n=== plan zapisu (bez confirm) ===")
            dry = await session.call_tool(
                "intake_commit",
                {
                    "item_id": 6,
                    "location": "A/A1",
                    "parts": [
                        {
                            "name": "ESP32-S3-DevKitC-1 (N16R8)",
                            "mpn": "ESP32-S3-N16R8",
                            "category": "Mikrokontrolery i płytki",
                            "amount": 1,
                            "description": "16 MB flash, 8 MB PSRAM, 2× USB-C",
                        }
                    ],
                },
            )
            for block in dry.content:
                if getattr(block, "type", "") == "text":
                    print(block.text)

            print("\n=== nieistniejąca lokalizacja ===")
            bad = await session.call_tool(
                "intake_commit",
                {
                    "item_id": 6,
                    "location": "Z/Z9",
                    "parts": [{"name": "cokolwiek"}],
                },
            )
            for block in bad.content:
                if getattr(block, "type", "") == "text":
                    print(block.text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
