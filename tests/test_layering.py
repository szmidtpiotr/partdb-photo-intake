"""Weryfikacja z issue #11: klient REST ma być jedynym punktem styku.

Reguła 4 z `docs/02-architektura.md`. Test celowo patrzy na źródła, a nie na
zaimportowane moduły — zależność wstrzyknięta importem złapie się nawet
wtedy, gdy nikt jej jeszcze nie wywołał.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "bom_agent"
CLIENT = "partdb_client.py"

# Biblioteki HTTP: poza klientem nie ma po nie sięgać żaden moduł.
HTTP_LIBS = {"httpx", "requests", "aiohttp", "urllib", "urllib3", "http"}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def modules_except_client() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if p.name != CLIENT)


def test_only_partdb_client_imports_an_http_library():
    offenders = {
        path.relative_to(PACKAGE).as_posix(): sorted(imported_modules(path) & HTTP_LIBS)
        for path in modules_except_client()
        if imported_modules(path) & HTTP_LIBS
    }
    assert offenders == {}, (
        "Biblioteka HTTP poza partdb_client.py łamie regułę 4 "
        f"z docs/02-architektura.md: {offenders}"
    )


def test_partdb_client_actually_is_the_module_that_talks_http():
    assert "httpx" in imported_modules(PACKAGE / CLIENT)


# Trasy REST Part-DB. Nasze własne trasy (`/api/intake/...`) są czymś innym
# i mają prawo żyć w `web.py` — reguła dotyczy adresów CUDZEGO API.
PARTDB_ROUTES = (
    "/api/parts",
    "/api/part_lots",
    "/api/storage_locations",
    "/api/categories",
    "/api/info_providers",
    "/api/attachments",
    "/api/projects",
)


def test_no_module_outside_client_builds_partdb_api_paths():
    offenders = {
        path.relative_to(PACKAGE).as_posix(): [
            route
            for route in PARTDB_ROUTES
            if route in path.read_text(encoding="utf-8")
        ]
        for path in modules_except_client()
        if any(route in path.read_text(encoding="utf-8") for route in PARTDB_ROUTES)
    }
    assert offenders == {}, (
        f"Adresy REST Part-DB składane poza partdb_client.py: {offenders}"
    )
