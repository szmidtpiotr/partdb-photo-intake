"""Warsztat — etap 1, odczyt (#77, ADR-022)."""

import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bom_agent.config import load_settings
from bom_agent.db import Database
from bom_agent.intake.queue import IntakeQueue
from bom_agent.partdb_client import PartDbClient, PartDbError, PartDbStatus
from bom_agent.warsztat import build_snapshot, media_url, quantity
from bom_agent.web import create_app

BASE = "https://warsztat.example"
PWA = Path(__file__).resolve().parent.parent / "pwa" / "warsztat"

CATEGORIES = [
    {"@id": "/api/categories/1", "id": 1, "name": "Moduły", "parent": None},
    {
        "@id": "/api/categories/2",
        "id": 2,
        "name": "Komunikacja",
        "parent": {"@id": "/api/categories/1"},
    },
    {"@id": "/api/categories/3", "id": 3, "name": "Przewody i złącza", "parent": None},
]
LOCATIONS = [
    {
        "@id": "/api/storage_locations/10",
        "id": 10,
        "name": "Karton A",
        "full_path": "Karton A",
        "parent": None,
    },
    {
        "@id": "/api/storage_locations/11",
        "id": 11,
        "name": "Kuweta A3",
        "full_path": "Karton A → Kuweta A3",
        "parent": {"@id": "/api/storage_locations/10"},
        "master_picture_attachment": {"media_url": "/media/storage/11/foto.jpg"},
    },
]
PARTS = [
    {
        "id": 1,
        "name": "ESP32 DevKit",
        "category": {"@id": "/api/categories/2", "id": 2, "name": "Komunikacja"},
        "partLots": [
            {
                "amount": 2,
                "instock_unknown": False,
                "storage_location": {"@id": "/api/storage_locations/11"},
            }
        ],
        "master_picture_attachment": {"media_url": f"{BASE}/api/intake/43/photo"},
        "attachments": [
            {"name": "Datasheet ESP32", "media_url": "/media/part/1/ds.pdf"}
        ],
        "needs_review": True,
        "description": "Wi-Fi i Bluetooth",
    },
    {
        "id": 2,
        "name": "Przewody Dupont",
        "category": {"@id": "/api/categories/3", "id": 3, "name": "Przewody i złącza"},
        "partLots": [{"amount": 0, "instock_unknown": True, "needs_refill": True}],
    },
    {
        "id": 3,
        "name": "Moduł katalogowy",
        "category": {"@id": "/api/categories/1", "id": 1},
        "partLots": [],
    },
]


# --- tłumaczenia -------------------------------------------------------------


def test_quantity_has_three_shapes_and_a_catalog_entry():
    assert quantity([{"amount": 4, "instock_unknown": False}]) == {"k": "exact", "n": 4}
    assert quantity([{"amount": 200, "instock_unknown": True}]) == {
        "k": "approx",
        "n": 200,
    }
    assert quantity([{"amount": 0, "instock_unknown": True}]) == {
        "k": "some",
        "n": None,
    }
    assert quantity([]) == {"k": "none", "n": None}


def test_media_url_makes_own_photos_relative_and_proxies_partdb_files():
    assert (
        media_url(f"{BASE}/api/intake/5/photo", BASE, 160)
        == "/api/intake/5/photo?w=160"
    )
    assert media_url(f"{BASE}/api/intake/5/photo?crop=0,0,0.5,0.5", BASE, 160) == (
        "/api/intake/5/photo?crop=0,0,0.5,0.5&w=160"
    )
    assert (
        media_url("/media/part/1/a.jpg", BASE)
        == "/api/warsztat/media?path=/media/part/1/a.jpg"
    )
    assert media_url(None, BASE) is None


def test_snapshot_counts_subcategories_into_the_parent_and_codes_boxes():
    snap = build_snapshot(PARTS, CATEGORIES, LOCATIONS, BASE)
    cats = {c["id"]: c for c in snap["categories"]}
    assert cats[1]["total"] == 2  # katalogowy + ESP32 z podkategorii
    assert cats[2]["total"] == 1

    boxes = {b["id"]: b for b in snap["boxes"]}
    assert boxes[11]["code"] == "A/A3"
    assert boxes[11]["path"] == "Karton A › Kuweta A3"
    assert boxes[11]["parent"] == 10
    assert boxes[11]["n"] == 1
    assert boxes[11]["photo"].startswith("/api/warsztat/media?path=/media/storage/11/")

    rows = {p["id"]: p for p in snap["parts"]}
    assert rows[1]["locs"] == [{"box": 11, "n": 2}]
    assert rows[1]["thumb"] == "/api/intake/43/photo?w=160"
    assert rows[1]["chk"] is True and rows[1]["ds"] is True
    assert rows[2]["low"] is True and rows[2]["dsn"] is True
    assert rows[3]["q"]["k"] == "none"


# --- API ------------------------------------------------------------------------


class StubPartDb:
    async def ping(self) -> PartDbStatus:
        return PartDbStatus(reachable=True, authorized=True, total_parts=3)

    async def list_parts(self):
        return PARTS

    async def list_categories(self):
        return CATEGORIES

    async def list_storage_locations(self):
        return LOCATIONS

    async def list_attachment_types(self):
        return [
            {"@id": "/api/attachment_types/1", "name": "Datasheet"},
            {"@id": "/api/attachment_types/2", "name": "Zdjęcie"},
        ]

    async def get_part(self, part_id):
        if int(part_id) != 1:
            raise PartDbError("nie ma", status=404)
        return {
            **PARTS[0],
            "comment": "Nadruk nieczytelny",
            "manufacturer_product_number": "ESP32-WROOM-32",
            "master_picture_attachment": {"@id": "/api/attachments/8"},
            "attachments": [
                {
                    "@id": "/api/attachments/7",
                    "id": 7,
                    "attachment_type": "/api/attachment_types/2",
                    "media_url": "/media/part/1/kat.jpg",
                },
                {
                    "@id": "/api/attachments/8",
                    "id": 8,
                    "attachment_type": "/api/attachment_types/2",
                    "media_url": f"{BASE}/api/intake/43/photo",
                },
                {
                    "@id": "/api/attachments/9",
                    "id": 9,
                    "attachment_type": "/api/attachment_types/1",
                    "media_url": "/media/part/1/ds.pdf",
                    "name": "DS",
                },
            ],
            "parameters": [{"@id": "/api/parameters/5"}],
            "orderdetails": [{"@id": "/api/orderdetails/6"}],
            "category": {
                "id": 2,
                "name": "Komunikacja",
                "full_path": "Moduły → Komunikacja",
            },
        }

    async def get(self, iri):
        if iri.endswith("parameters/5"):
            return {"name": "Napięcie", "formatted": "3.3 V"}
        return {
            "supplier_product_url": "https://botland.example/esp32",
            "supplier": {"name": "Botland"},
        }

    async def fetch_media(self, path):
        if not path.startswith("/media/"):
            raise PartDbError("tylko /media/")
        return b"%PDF", "application/pdf"

    async def aclose(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def client(tmp_path):
    db = Database(tmp_path / "bom.db")
    db.migrate()
    queue = IntakeQueue(db, tmp_path / "photos")
    app = create_app(
        load_settings({"PARTDB_API_TOKEN": "tcp_test", "PUBLIC_BASE_URL": BASE}),
        client=StubPartDb(),
        queue=queue,
    )
    with TestClient(app) as tc:
        yield tc


def test_snapshot_route(client):
    data = client.get("/api/warsztat/snapshot").json()
    assert len(data["parts"]) == 3
    assert {c["name"] for c in data["categories"]} == {
        "Moduły",
        "Komunikacja",
        "Przewody i złącza",
    }


def test_part_card_puts_the_list_thumbnail_first_and_reads_shop_and_parameters(client):
    card = client.get("/api/warsztat/parts/1").json()
    assert card["photos"][0]["url"] == "/api/intake/43/photo?w=1024"
    assert card["photos"][1]["url"].startswith(
        "/api/warsztat/media?path=/media/part/1/kat.jpg"
    )
    assert card["datasheet"] == "file"
    assert card["shops"] == [
        {"url": "https://botland.example/esp32", "name": "Botland"}
    ]
    assert {"k": "Napięcie", "v": "3.3 V"} in card["tech"]
    assert {"k": "Nr katalogowy", "v": "ESP32-WROOM-32"} in card["tech"]
    assert card["cat_path"] == "Moduły › Komunikacja"
    assert card["notes"] == "Nadruk nieczytelny"


def test_unknown_part_is_404(client):
    assert client.get("/api/warsztat/parts/999").status_code == 404


def test_media_proxy_refuses_paths_outside_media(client):
    assert (
        client.get(
            "/api/warsztat/media", params={"path": "/media/part/1/ds.pdf"}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/warsztat/media", params={"path": "/api/parts"}).status_code
        == 400
    )


async def test_client_fetch_media_refuses_traversal():
    async with PartDbClient("http://partdb.invalid", "t") as real:
        with pytest.raises(PartDbError):
            await real.fetch_media("/media/../config/.env")
        with pytest.raises(PartDbError):
            await real.fetch_media("/api/parts")


def test_intake_photo_serves_a_thumbnail_and_rejects_odd_sizes(client):
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1500), "orange").save(buffer, "JPEG")
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", buffer.getvalue(), "image/jpeg")}
    ).json()
    small = client.get(f"{item['photo_url']}?w=160")
    assert small.status_code == 200
    assert max(Image.open(io.BytesIO(small.content)).size) == 160
    assert client.get(f"{item['photo_url']}?w=999").status_code == 400


# --- powłoka ---------------------------------------------------------------------


def test_warsztat_page_is_served_and_versions_agree(client):
    page = client.get("/warsztat/")
    assert page.status_code == 200
    html = (PWA / "index.html").read_text(encoding="utf-8")
    versions = set(re.findall(r"/warsztat/warsztat\.(?:css|js)\?v=(\d+)", html))
    js = (PWA / "warsztat.js").read_text(encoding="utf-8")
    app_version = re.search(r"const APP_VERSION = (\d+);", js).group(1)
    assert versions == {app_version}


def test_frontend_escapes_text_from_the_database():
    # Nazwy i opisy pochodzą z rozpoznania zdjęć — wpisany do nich znacznik
    # HTML nie może wykonać się na telefonie.
    js = (PWA / "warsztat.js").read_text(encoding="utf-8")
    assert "${esc(p.name)}" in js
    # Wewnątrz `encodeURIComponent(...)` nazwa jest bezpieczna; w treści HTML
    # nie może stać goła.
    assert ">${p.name}" not in js and "${p.name}<" not in js


# --- jedna aplikacja (ADR-022, etap 4) ---------------------------------------


def test_home_is_warsztat_and_intake_lives_under_dodaj(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "/warsztat/warsztat.js" in home.text

    # „Dodaj” to zakładka Warsztatu; stary ekran zostaje pod /index.html.
    add = client.get("/dodaj", follow_redirects=False)
    assert add.status_code == 302 and add.headers["location"] == "/#dodaj"
    assert "/app.js" in client.get("/index.html").text

    old = client.get("/warsztat/", follow_redirects=False)
    assert old.status_code == 301 and old.headers["location"] == "/"


def test_both_pages_share_the_same_tabs():
    intake = (PWA.parent / "index.html").read_text(encoding="utf-8")
    for href in ('href="/"', 'href="/#przegladaj"', 'href="/#listy"', 'href="/dodaj"'):
        assert href in intake
    js = (PWA / "warsztat.js").read_text(encoding="utf-8")
    assert "'#przegladaj': 'browse'" in js and "'#listy': 'lists'" in js
    assert "'#dodaj': 'add'" in js


def test_shared_photos_land_on_the_add_page():
    # Udostępnienie zdjęcia z Lensa ląduje w Warsztacie, który z parametrów
    # adresu przełącza się na zakładkę „Dodaj”.
    sw = (PWA.parent / "sw.js").read_text(encoding="utf-8")
    assert "new URL('/', self.location.origin)" in sw
    assert "handleShareParams" in (PWA / "warsztat.js").read_text(encoding="utf-8")
