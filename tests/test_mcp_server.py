"""Weryfikacja z #12: serwer daje się wylistować i narzędzie odpowiada.

Testy chodzą po API FastMCP (`list_tools` / `call_tool`), a nie po samej
funkcji — inaczej nie wykryłyby, że narzędzie w ogóle nie zostało
zarejestrowane, a to jest dokładnie ta awaria, którą widzi użytkownik.
"""

import pytest

from bom_agent import mcp_server
from bom_agent.config import Settings
from bom_agent.intake.queue import IntakeItem
from bom_agent.partdb_client import PartDbUnavailable


class FakeClient:
    def __init__(self, parts=None, error=None):
        self.parts = parts or []
        self.error = error
        self.calls = 0

    async def list_parts(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.parts


@pytest.fixture
def fake_client(monkeypatch):
    def install(client):
        monkeypatch.setattr(mcp_server, "get_client", lambda: client)
        return client

    return install


class NoProposals:
    """Atrapa magazynu propozycji: nic nie czeka na zatwierdzenie."""

    async def items_with_pending(self):
        return set()


async def call(name: str, arguments: dict) -> str:
    result = await mcp_server.mcp.call_tool(name, arguments)
    assert not result.is_error, result.content
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def test_server_exposes_inventory_digest():
    tools = await mcp_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "inventory_digest" in names

    tool = next(t for t in tools if t.name == "inventory_digest")
    assert "scope" in tool.input_schema["properties"]
    # Opis trafia do agenta i to on decyduje, czy narzędzie zostanie użyte
    # poprawnie — pusty opis jest błędem, nie kosmetyką.
    assert tool.description and "magazyn" in tool.description.lower()


async def test_inventory_digest_returns_a_readable_dump(fake_client):
    fake_client(
        FakeClient(
            parts=[
                {
                    "name": "ESP32-WROOM-32 DevKit v1",
                    "category": {"full_path": "Mikrokontrolery i płytki"},
                    "partLots": [
                        {
                            "storage_location": {"full_path": "Karton A → Kuweta A1"},
                            "amount": 6,
                            "instock_unknown": False,
                            "needs_refill": False,
                        }
                    ],
                    "total_instock": 6,
                }
            ]
        )
    )

    text = await call("inventory_digest", {})
    assert "MAGAZYN" in text
    assert "ESP32-WROOM-32 DevKit v1" in text
    assert "A/A1" in text


async def test_inventory_digest_passes_scope_through(fake_client):
    fake_client(
        FakeClient(
            parts=[
                {
                    "name": "ESP32",
                    "category": {"full_path": "Płytki"},
                    "partLots": [{"amount": 1}],
                    "total_instock": 1,
                },
                {
                    "name": "HC-SR04",
                    "category": {"full_path": "Czujniki"},
                    "partLots": [{"amount": 5}],
                    "total_instock": 5,
                },
            ]
        )
    )

    text = await call("inventory_digest", {"scope": "czujniki"})
    assert "HC-SR04" in text
    assert "ESP32" not in text


class WritingClient(FakeClient):
    """Klient, który przyjmuje zapisy — pod `intake_commit`."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.created: list[tuple[str, dict]] = []
        self._next_id = 200

    async def list_storage_locations(self):
        return []

    async def list_categories(self):
        return [{"@id": "/api/categories/3", "name": "Czujniki"}]

    async def list_attachment_types(self):
        return [{"@id": "/api/attachment_types/2", "name": "Zdjęcie"}]

    async def create(self, resource, payload):
        self._next_id += 1
        self.created.append((resource, payload))
        return {"@id": f"/api/{resource}/{self._next_id}", **payload}

    async def create_part(self, payload):
        return await self.create("parts", payload)

    async def create_part_lot(self, payload):
        return await self.create("part_lots", payload)

    async def create_attachment(self, payload):
        return await self.create("attachments", payload)

    async def patch(self, iri, payload):
        return {"@id": iri, **payload}


class FakeQueue:
    def __init__(self, item):
        self.item = item
        self.committed: list[int] = []

    async def get(self, item_id):
        return self.item

    async def commit(self, item_id, *, result=None, partdb_refs=None):
        self.committed.append(item_id)
        return self.item


async def test_intake_commit_attaches_the_queue_photo_to_a_new_part(
    fake_client, monkeypatch
):
    """#64: adres zdjęcia bierze się z PUBLIC_BASE_URL, bo otwiera go
    przeglądarka po stronie Part-DB, a nie proces bom-agent."""
    client = fake_client(WritingClient())
    item = IntakeItem(
        id=17,
        kind="photo",
        status="pending",
        created_at="2026-08-14T10:00:00+00:00",
        payload_path="/data/intake/photos/17.jpg",
    )
    queue = FakeQueue(item)
    monkeypatch.setattr(mcp_server, "get_queue", lambda: queue)
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: Settings(public_base_url="https://warsztat.example"),
    )

    text = await call(
        "intake_commit",
        {
            "item_id": 17,
            "parts": [{"name": "HC-SR501", "category": "Czujniki", "amount": 1}],
            "confirm": True,
        },
    )

    attachment = dict(client.created)["attachments"]
    assert attachment["url"] == "https://warsztat.example/api/intake/17/photo"
    assert attachment["name"] == "Zdjęcie własne (intake #17)"
    assert queue.committed == [17]
    assert "Zapisane" in text


async def test_intake_commit_plan_announces_the_photo_without_writing(
    fake_client, monkeypatch
):
    client = fake_client(WritingClient())
    item = IntakeItem(
        id=17,
        kind="photo",
        status="pending",
        created_at="2026-08-14T10:00:00+00:00",
        payload_path="/data/intake/photos/17.jpg",
    )
    monkeypatch.setattr(mcp_server, "get_queue", lambda: FakeQueue(item))
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: Settings(public_base_url="https://warsztat.example"),
    )

    text = await call(
        "intake_commit",
        {
            "item_id": 17,
            "parts": [{"name": "HC-SR501", "category": "Czujniki"}],
        },
    )

    assert client.created == []
    assert "https://warsztat.example/api/intake/17/photo" in text


async def test_phone_hint_pointing_at_a_nonexistent_box_registers_anyway(
    fake_client, monkeypatch
):
    """Dyktowanie mówi „kuweta B3", zanim taki pojemnik powstanie (ADR-017).
    Odmowa zapisu kosztowałaby cały spis; nieprzypisany stan jest właśnie
    listą roboczą na sesję sortowania."""
    fake_client(WritingClient())
    item = IntakeItem(
        id=19,
        kind="voice",
        status="pending",
        created_at="2026-08-14T10:00:00+00:00",
        payload_text="kuweta B3, dużo 100nF 0805",
        location_hint="Kuweta B3",
    )
    monkeypatch.setattr(mcp_server, "get_queue", lambda: FakeQueue(item))

    text = await call(
        "intake_commit",
        {
            "item_id": 19,
            "parts": [
                {
                    "name": "Kondensator 100nF 0805",
                    "bucket": "dużo",
                    "category": "Bierne",
                }
            ],
        },
    )

    assert "BEZ PRZYPISANIA" in text
    assert "Kuweta B3" in text
    assert "~200 (dużo)" in text


async def test_location_given_explicitly_still_has_to_exist(fake_client, monkeypatch):
    """Pojemnika nie wymyślamy: wpisany wprost, a nieistniejący, to błąd."""
    fake_client(WritingClient())
    item = IntakeItem(
        id=19,
        kind="voice",
        status="pending",
        created_at="2026-08-14T10:00:00+00:00",
        payload_text="dużo 100nF",
    )
    monkeypatch.setattr(mcp_server, "get_queue", lambda: FakeQueue(item))

    text = await call(
        "intake_commit",
        {
            "item_id": 19,
            "parts": [{"name": "Kondensator 100nF", "category": "Bierne"}],
            "location": "Kuweta Z9",
        },
    )

    assert "Nie zapisano" in text


async def test_partdb_failure_is_reported_not_hidden(fake_client):
    """Agent ma zobaczyć awarię, a nie pusty magazyn — pusty zrzut czytałby
    jako „nic nie masz" i zaczął planować zakupy."""
    fake_client(FakeClient(error=PartDbUnavailable("Part-DB nieosiągalny")))

    text = await call("inventory_digest", {})
    assert "Nie udało się pobrać stanu magazynu" in text
    assert "nie zgaduj" in text.lower()


# --- kilka ujęć jednej rzeczy (#66) ----------------------------------------


def _grouped_item(tmp_path):
    """Pozycja z dwoma ujęciami — tak, jak buduje ją kolejka."""
    from PIL import Image as PilImage

    from bom_agent.intake.queue import Shot

    paths = []
    for name in ("gora.jpg", "spod.jpg"):
        path = tmp_path / name
        PilImage.new("RGB", (800, 600), (30, 40, 50)).save(path, "JPEG")
        paths.append(path)

    return IntakeItem(
        id=17,
        kind="photo",
        status="pending",
        created_at="2026-08-14T10:00:00+00:00",
        payload_path=str(paths[0]),
        shots=(
            Shot(id=17, payload_path=str(paths[0])),
            Shot(id=18, payload_path=str(paths[1])),
        ),
    )


async def test_intake_pending_shows_all_shots_as_one_item(tmp_path, monkeypatch):
    """Ujęcia jednej rzeczy mają przyjść razem — rozdzielone wyglądałyby
    jak dwa różne moduły i skończyły dwoma zapisami."""
    item = _grouped_item(tmp_path)

    class Queue:
        async def list(self, *, status=None, limit=50):
            return [item]

        async def counts(self):
            return {"pending": 1, "processing": 0, "committed": 0, "rejected": 0}

    monkeypatch.setattr(mcp_server, "get_queue", lambda: Queue())
    # Od ADR-021 narzędzie pomija pozycje, które mają już propozycje do
    # zatwierdzenia — atrapa mówi „żadna nie ma".
    monkeypatch.setattr(mcp_server, "get_proposals", lambda: NoProposals())
    result = await mcp_server.mcp.call_tool("intake_pending", {"limit": 5})

    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    images = [b for b in result.content if not hasattr(b, "text")]
    assert "2 ujęcia tej samej rzeczy" in text
    assert "ujęcie 1" in text and "ujęcie 2" in text
    assert len(images) == 2


async def test_intake_pending_shows_the_users_note_next_to_the_photo(
    tmp_path, monkeypatch
):
    """Notatka „BC557, 10 szt" ma dojść do agenta razem ze zdjęciem (#75) —
    liczby sztuk w woreczku nie wyczyta z obrazu."""
    from dataclasses import replace

    item = replace(_grouped_item(tmp_path), payload_text="BC557, 10 szt")

    class Queue:
        async def list(self, *, status=None, limit=50):
            return [item]

        async def counts(self):
            return {"pending": 1, "processing": 0, "committed": 0, "rejected": 0}

    class Client:
        async def list_categories(self):
            return [{"name": "Tranzystory"}]

    monkeypatch.setattr(mcp_server, "get_queue", lambda: Queue())
    monkeypatch.setattr(mcp_server, "get_proposals", lambda: NoProposals())
    # Własna atrapa klienta: współdzielony `_client` z poprzedniego testu
    # jest przywiązany do zamkniętej już pętli zdarzeń.
    monkeypatch.setattr(mcp_server, "get_client", lambda: Client())
    result = await mcp_server.mcp.call_tool("intake_pending", {"limit": 5})

    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    assert "notatka użytkownika: BC557, 10 szt" in text


async def test_intake_commit_attaches_every_shot_and_uses_the_first_as_thumbnail(
    fake_client, tmp_path, monkeypatch
):
    client = fake_client(WritingClient())
    monkeypatch.setattr(
        mcp_server, "get_queue", lambda: FakeQueue(_grouped_item(tmp_path))
    )
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: Settings(public_base_url="https://warsztat.example"),
    )

    await call(
        "intake_commit",
        {
            "item_id": 17,
            "parts": [{"name": "HC-SR501", "category": "Czujniki", "amount": 1}],
            "confirm": True,
        },
    )

    attachments = [payload for name, payload in client.created if name == "attachments"]
    assert [a["url"] for a in attachments] == [
        "https://warsztat.example/api/intake/17/photo",
        "https://warsztat.example/api/intake/18/photo",
    ]
    # Nazwy muszą się różnić: Part-DB odrzuca powtórzoną nazwą załącznika
    # w obrębie części (422: „This value is already used").
    assert len({a["name"] for a in attachments}) == 2


# --- przypisywanie pojemnika (#62) -----------------------------------------


class PlacingClient(WritingClient):
    """Klient z jednym pojemnikiem i jedną rzeczą leżącą luzem."""

    async def list_storage_locations(self):
        return [
            {
                "@id": "/api/storage_locations/1",
                "name": "Kuweta A1",
                "full_path": "Karton A → Kuweta A1",
            }
        ]

    async def list_parts(self):
        return [
            {
                "@id": "/api/parts/7",
                "id": 7,
                "name": "HC-SR04",
                "category": {"@id": "/api/categories/3", "name": "Czujniki"},
                "partLots": [
                    {
                        "@id": "/api/part_lots/10",
                        "id": 10,
                        "amount": 4,
                        "instock_unknown": False,
                    }
                ],
            }
        ]

    async def patch(self, iri, payload):
        self.created.append(("patch", {"iri": iri, **payload}))
        return {"@id": iri, **payload}


async def test_assign_location_is_registered_with_the_other_tools():
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert "assign_location" in names


async def test_assign_location_plan_writes_nothing(fake_client):
    client = fake_client(PlacingClient())

    text = await call("assign_location", {"location": "A/A1", "parts": ["HC-SR04"]})

    assert "Karton A → Kuweta A1" in text
    assert "confirm=true" in text
    assert client.created == []


async def test_assign_location_with_confirm_moves_the_stock(fake_client):
    client = fake_client(PlacingClient())

    text = await call(
        "assign_location",
        {"location": "A/A1", "parts": ["HC-SR04"], "confirm": True},
    )

    assert client.created == [
        (
            "patch",
            {
                "iri": "/api/part_lots/10",
                "storage_location": "/api/storage_locations/1",
            },
        )
    ]
    assert "Przypisane do: Karton A → Kuweta A1" in text


async def test_assign_location_refuses_a_box_that_does_not_exist(fake_client):
    client = fake_client(PlacingClient())

    text = await call(
        "assign_location",
        {"location": "Kuweta Z9", "parts": ["HC-SR04"], "confirm": True},
    )

    assert "Nie przypisano" in text
    assert client.created == []


# --- dane od dostawcy (#67) -------------------------------------------------


class ProviderClient(WritingClient):
    """Klient z jedną częścią i jednym trafieniem u LCSC."""

    async def list_parts(self):
        return [
            {
                "@id": "/api/parts/5",
                "id": 5,
                "name": "NE555",
                "manufacturer_product_number": "NE555",
                "category": {"@id": "/api/categories/3", "name": "Układy scalone"},
                "partLots": [],
                "parameters": [],
            }
        ]

    async def list_attachments(self):
        return []

    async def list_info_providers(self):
        return [{"key": "lcsc"}]

    async def search_info_providers(self, keyword, providers=None):
        return [
            {
                "provider_key": "lcsc",
                "provider_id": "C5125085",
                "name": "NE555",
                "mpn": "NE555",
            }
        ]

    async def info_provider_details(self, provider_key, provider_id):
        return {
            "images": [{"url": "https://assets.lcsc.com/ne555.jpg"}],
            "datasheets": [{"url": "https://datasheet.lcsc.com/ne555.pdf"}],
            "parameters": [{"name": "Package", "value_text": "DIP-8"}],
        }

    async def create_parameter(self, payload):
        return await self.create("parameters", payload)


async def test_enrich_part_is_registered():
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert "enrich_part" in names


async def test_enrich_part_plan_writes_nothing(fake_client):
    client = fake_client(ProviderClient())

    text = await call("enrich_part", {"parts": ["NE555"]})

    assert "zdjęcie katalogowe" in text
    assert "confirm=true" in text
    assert client.created == []


async def test_enrich_part_with_confirm_attaches_and_downloads(fake_client):
    client = fake_client(ProviderClient())

    text = await call("enrich_part", {"parts": ["NE555"], "confirm": True})

    attachments = [p for r, p in client.created if r == "attachments"]
    assert len(attachments) == 2
    assert all(a["upload"]["downloadUrl"] for a in attachments)
    assert "Uzupełnione" in text
