"""Spójność wersji plików PWA (#60).

Zmierzone na sprzęcie: serwer miał nowy plik, a zainstalowana aplikacja
tygodniami wykonywała stary kod, bo adres zasobu się nie zmienił. Numer
wersji musi być podbity w trzech miejscach naraz — `index.html`, `sw.js`
i `APP_VERSION` — a to jest dokładnie ten rodzaj kroku, o którym się
zapomina. Test jest tańszy niż kolejna sesja diagnozowania ducha.
"""

import re
from pathlib import Path

PWA = Path(__file__).resolve().parent.parent / "pwa"

INDEX = (PWA / "index.html").read_text(encoding="utf-8")
APP = (PWA / "app.js").read_text(encoding="utf-8")
SW = (PWA / "sw.js").read_text(encoding="utf-8")


def app_version() -> int:
    return int(re.search(r"const APP_VERSION = (\d+);", APP).group(1))


def test_all_asset_versions_match_app_version():
    version = app_version()
    referenced = set(re.findall(r"\?v=(\d+)", INDEX + SW))
    assert referenced == {str(version)}, (
        f"APP_VERSION={version}, a w plikach są znaczniki {sorted(referenced)} — "
        "telefon wykona stary kod"
    )


def test_service_worker_cache_name_carries_the_same_version():
    version = app_version()
    cache = re.search(r"const CACHE = '([^']+)'", SW).group(1)
    assert cache == f"bom-intake-v{version}", (
        f"Pula cache {cache!r} nie odpowiada wersji {version} — stara powłoka "
        "zostanie w telefonie, bo nic jej nie unieważni"
    )


def test_every_cached_asset_exists_on_disk():
    """`cache.addAll` jest atomowe: jeden brakujący plik wywala instalację
    service workera w całości, a aplikacja zostaje bez trybu offline."""
    shell = re.search(r"const SHELL = \[(.*?)\];", SW, re.S).group(1)
    for asset in re.findall(r"'([^']+)'", shell):
        path = asset.split("?")[0].lstrip("/")
        # '/' i '/dodaj' to trasy serwera (Warsztat i intake), nie pliki.
        if path in ("", "dodaj"):
            continue
        assert (PWA / path).is_file(), f"{asset} jest w SHELL, ale nie ma go w repo"


def test_share_target_matches_what_the_service_worker_handles():
    """Cel udostępniania (#65) jest umową między manifestem a service
    workerem: adres i nazwa pola muszą się zgadzać co do znaku, bo Android
    wyśle dokładnie to, co stoi w manifeście, a odbierze to nasz kod."""
    import json

    manifest = json.loads((PWA / "manifest.webmanifest").read_text(encoding="utf-8"))
    share = manifest["share_target"]

    assert share["method"] == "POST"
    assert share["enctype"] == "multipart/form-data"
    assert f"pathname === '{share['action']}'" in SW

    field = share["params"]["files"][0]["name"]
    assert f"form.getAll('{field}')" in SW


def test_outbox_is_loaded_before_the_app():
    """`app.js` woła `Outbox` od pierwszego rysowania kolejki."""
    assert INDEX.index("outbox.js") < INDEX.index("app.js")
