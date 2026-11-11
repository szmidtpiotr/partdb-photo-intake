#!/usr/bin/env python3
"""Smoke na ŻYWEJ instancji Part-DB — tylko odczyt, niczego nie zapisuje.

Testy jednostkowe chodzą na `MockTransport`, więc nie powiedzą, czy realna
instancja nadal zwraca to, na czym stoi klient. To jest ten drugi test.

    PARTDB_URL=http://localhost:8090 \\
    PARTDB_API_TOKEN=tcp_... python scripts/smoke_partdb.py
"""

from __future__ import annotations

import asyncio
import sys

from bom_agent.config import load_settings
from bom_agent.partdb_client import PartDbClient, PartDbError


async def main() -> int:
    settings = load_settings()
    if not settings.has_partdb_token:
        print("Brak PARTDB_API_TOKEN — nie ma czym się uwierzytelnić")
        return 2

    async with PartDbClient(
        settings.partdb_url,
        settings.partdb_api_token,
        timeout=settings.partdb_timeout,
    ) as client:
        status = await client.ping()
        print(f"ping         : {status}")
        if not status.ok:
            return 1

        parts = await client.list_parts()
        lots = await client.list_part_lots()
        locations = await client.list_storage_locations()
        print(f"parts        : {len(parts)}")
        print(f"part_lots    : {len(lots)}")
        print(f"locations    : {len(locations)}")

        # Ustalenie 1 i 2: wpisy katalogowe (zaciągnięte z LCSC) nie mają
        # PartLot. Dyskryminatorem jest puste partLots, NIE total_instock == 0.
        catalog_only = [p for p in parts if not p.get("partLots")]
        empty_stock = [
            p for p in parts if p.get("partLots") and not p.get("total_instock")
        ]
        print(
            f"bez PartLot  : {len(catalog_only)} -> {[p['name'] for p in catalog_only]}"
        )
        print(f"stan 0, ale w kartonie: {len(empty_stock)}")

        # Ustalenie 3: dane demo nie mają ani jednej pozycji klasy B.
        class_b = [lot for lot in lots if lot.get("instock_unknown")]
        print(f"klasa B (instock_unknown): {len(class_b)}")

        if parts:
            sample = parts[0]
            print(
                f"przykład     : {sample['name']} ({sample.get('total_instock')} szt.)"
            )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except PartDbError as exc:
        print(f"BŁĄD: {exc}")
        sys.exit(1)
