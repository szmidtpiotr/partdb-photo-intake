"""TDD: Issue #73 — etap propozycji między rozpoznaniem a zapisem (ADR-021).

Rozpoznanie zapisuje propozycje, nie części. Magazyn rusza dopiero ✓
człowieka — dzięki temu rozpoznawanie bez nadzoru (#75) nie może niczego
zepsuć, a zatwierdzić da się z telefonu.
"""

import pytest
from fastapi.testclient import TestClient

from bom_agent import mcp_server
from bom_agent.config import Settings, load_settings
from bom_agent.db import Database
from bom_agent.intake.commit import IntakeCommitter
from bom_agent.intake.proposals import (
    ProposalError,
    ProposalReviewer,
    ProposalStore,
)
from bom_agent.intake.queue import IntakeError, IntakeQueue
from bom_agent.partdb_client import PartDbStatus
from bom_agent.web import create_app


def _jpeg() -> bytes:
    """Prawdziwy JPEG, nie atrapa bajtów: `intake_pending` przepuszcza
    zdjęcia przez Pillow, a atrapa kończy się „Truncated File Read"."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1200, 900), (40, 60, 80)).save(buffer, "JPEG")
    return buffer.getvalue()


JPEG = _jpeg()

LOCATIONS = [
    {
        "@id": "/api/storage_locations/2",
        "name": "Kuweta A1",
        "full_path": "Karton A → Kuweta A1",
    }
]
CATEGORIES = [{"@id": "/api/categories/3", "name": "Czujniki"}]
ATTACHMENT_TYPES = [{"@id": "/api/attachment_types/2", "name": "Zdjęcie"}]


class FakePartDb:
    """Podstawka pod `PartDbClient` — zapisuje, co dostała."""

    def __init__(self, parts=None):
        self.parts = list(parts or [])
        self.created: list[tuple[str, dict]] = []
        self.patched: list[tuple[str, dict]] = []
        self._next_id = 300

    async def ping(self) -> PartDbStatus:
        return PartDbStatus(
            reachable=True, authorized=True, total_parts=len(self.parts)
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass

    async def list_storage_locations(self):
        return LOCATIONS

    async def list_categories(self):
        return CATEGORIES

    async def list_parts(self):
        return self.parts

    async def list_attachment_types(self):
        return ATTACHMENT_TYPES

    async def create_attachment(self, payload):
        return await self.create("attachments", payload)

    async def create_part(self, payload):
        part = await self.create("parts", payload)
        # Nowa część natychmiast staje się częścią bazy — inaczej druga
        # propozycja z tego samego zdjęcia nie mogłaby się z nią porównać.
        self.parts.append(part)
        return part

    async def create_part_lot(self, payload):
        return await self.create("part_lots", payload)

    async def create(self, resource, payload):
        self._next_id += 1
        self.created.append((resource, payload))
        return {"@id": f"/api/{resource}/{self._next_id}", **payload}

    async def patch(self, iri, payload):
        self.patched.append((iri, payload))
        return {"@id": iri, **payload}

    def names(self, resource="parts"):
        return [p.get("name") for r, p in self.created if r == resource]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "bom.db")
    database.migrate()
    return database


@pytest.fixture
def queue(db, tmp_path):
    return IntakeQueue(db, tmp_path / "photos")


@pytest.fixture
def store(db):
    return ProposalStore(db)


def payload(**kwargs):
    return {"name": "HC-SR04", "amount": 1, "category": "Czujniki", **kwargs}


async def photo_item(queue, **kwargs):
    return await queue.add_photo(JPEG, **kwargs)


def make_reviewer(store, queue, client):
    return ProposalReviewer(
        store,
        queue,
        IntakeCommitter(client),
        lambda shot_id: f"https://warsztat.example/api/intake/{shot_id}/photo",
    )


# ─── Zlecenie rozpoznania ────────────────────────────────────────────────────


async def test_recognition_is_requested_per_item(queue):
    """Przycisk „Rozpoznaj" to jawne zlecenie, nie zgoda na całą kolejkę."""
    a = await photo_item(queue)
    b = await photo_item(queue)

    await queue.request_recognition(a.id)

    requested = await queue.requested()
    assert [item.id for item in requested] == [a.id]
    assert b.id not in [item.id for item in requested]


async def test_request_can_be_taken_back(queue):
    item = await photo_item(queue)
    await queue.request_recognition(item.id)

    await queue.request_recognition(item.id, wanted=False)

    assert await queue.requested() == []
    assert (await queue.get(item.id)).recognize_requested_at is None


async def test_request_on_a_shot_marks_its_item(queue):
    """Ujęcie nie jest osobną rzeczą, więc nie ma własnego zlecenia (#66)."""
    item = await photo_item(queue)
    await queue.add_photo(JPEG, parent_id=item.id)
    shot_id = (await queue.get(item.id)).shots[1].id

    marked = await queue.request_recognition(shot_id)

    assert marked.id == item.id
    assert [i.id for i in await queue.requested()] == [item.id]


async def test_committed_item_cannot_be_queued_for_recognition(queue):
    item = await photo_item(queue)
    await queue.commit(item.id, result=[], partdb_refs=["/api/parts/1"])

    with pytest.raises(IntakeError, match="committed"):
        await queue.request_recognition(item.id)


# ─── Magazyn propozycji ──────────────────────────────────────────────────────


async def test_proposal_keeps_the_dedup_verdict_from_recognition(store, queue):
    """Werdykt zapisujemy przy rozpoznaniu, nie liczymy przy zatwierdzaniu —
    karta na telefonie ma pokazać „dołoży stan do X" bez pytania Part-DB."""
    item = await photo_item(queue)

    (proposal,) = await store.create(
        item.id,
        [
            (
                payload(),
                {
                    "action": "merge",
                    "existing": {"name": "HC-SR04", "iri": "/api/parts/9"},
                },
            )
        ],
    )

    card = proposal.as_dict()
    assert card["action"] == "merge"
    assert card["existing"]["name"] == "HC-SR04"
    assert card["status"] == "pending"


async def test_editable_fields_can_be_fixed_from_the_phone(store, queue):
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])

    edited = await store.edit(proposal.id, {"amount": 5, "note": "policzone"})

    assert edited.payload["amount"] == 5
    assert edited.payload["note"] == "policzone"


async def test_recognition_fields_are_not_editable_from_the_phone(store, queue):
    """Wycinek i rozstrzygnięcie dedupu to wynik rozpoznania. Ich zmiana
    palcem znaczyłaby, że rozpoznanie jest do wyrzucenia, nie do poprawki."""
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(crop=[0, 0, 0.5, 1]), {})])

    with pytest.raises(ProposalError, match="crop"):
        await store.edit(proposal.id, {"crop": [0, 0, 1, 1]})


async def test_renaming_invalidates_the_stored_verdict(store, queue):
    """„Dołoży stan do X" dotyczyło innej nazwy. Zostawienie werdyktu
    pokazywałoby na telefonie coś, co przy zatwierdzaniu już nie nastąpi."""
    item = await photo_item(queue)
    (proposal,) = await store.create(
        item.id,
        [
            (
                payload(),
                {"action": "merge", "existing": {"name": "X", "iri": "/api/parts/9"}},
            )
        ],
    )

    edited = await store.edit(proposal.id, {"name": "DHT22"})

    assert edited.as_dict()["action"] == "create"
    assert edited.as_dict()["existing"] is None


async def test_empty_name_is_refused(store, queue):
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])

    with pytest.raises(ProposalError, match="pusta"):
        await store.edit(proposal.id, {"name": "   "})


# ─── Zatwierdzanie ───────────────────────────────────────────────────────────


async def test_approval_writes_through_the_ordinary_committer(store, queue):
    """Zero drugiej ścieżki zapisu: część, stan i zdjęcie powstają tak samo
    jak przy `intake_commit`."""
    client = FakePartDb()
    item = await photo_item(queue, location_hint="A/A1")
    (proposal,) = await store.create(item.id, [(payload(), {})])

    approved, refs = await make_reviewer(store, queue, client).approve(proposal.id)

    assert approved.status == "approved"
    assert client.names("parts") == ["HC-SR04"]
    assert [r for r, _ in client.created].count("part_lots") == 1
    # Zdjęcie własne trafia do nowej części (#64).
    assert [r for r, _ in client.created].count("attachments") == 1
    assert any("/api/parts/" in ref for ref in refs)


async def test_pending_proposal_blocks_recognition_of_the_same_item(queue, store):
    """Pozycja z propozycjami czeka na człowieka, nie na rozpoznanie."""
    item = await photo_item(queue)
    await store.create(item.id, [(payload(), {})])

    assert await store.items_with_pending() == {item.id}


async def test_item_is_committed_only_after_the_last_decision(store, queue):
    client = FakePartDb()
    item = await photo_item(queue)
    first, second = await store.create(
        item.id, [(payload(), {}), (payload(name="DHT22"), {})]
    )
    reviewer = make_reviewer(store, queue, client)

    await reviewer.approve(first.id)
    assert (await queue.get(item.id)).status == "pending", (
        "pozycja z jedną decyzją do podjęcia nie jest jeszcze rozliczona"
    )

    await reviewer.approve(second.id)
    assert (await queue.get(item.id)).status == "committed"


async def test_all_rejected_means_the_item_is_rejected_not_committed(store, queue):
    """Nic nie weszło do magazynu, więc pozycja nie może udawać zapisanej —
    inaczej archiwum kłamałoby o stanie warsztatu."""
    client = FakePartDb()
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])

    await make_reviewer(store, queue, client).reject(proposal.id, "to nie ta część")

    refreshed = await queue.get(item.id)
    assert refreshed.status == "rejected"
    assert client.created == []


async def test_approved_item_records_names_and_refs_for_the_archive(store, queue):
    """Archiwum PWA (#69) pokazuje „HC-SR04 → Part-DB" — nazwa z propozycji,
    numer rekordu z tego, co powstało przy zatwierdzaniu."""
    client = FakePartDb()
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])

    await make_reviewer(store, queue, client).approve(proposal.id)

    records = (await queue.get(item.id)).part_records()
    assert [r["name"] for r in records] == ["HC-SR04"]
    assert records[0]["part_id"] is not None


async def test_second_approval_of_the_same_proposal_is_refused(store, queue):
    client = FakePartDb()
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])
    reviewer = make_reviewer(store, queue, client)
    await reviewer.approve(proposal.id)

    with pytest.raises(ProposalError, match="approved"):
        await reviewer.approve(proposal.id)


async def test_ambiguous_dedup_is_sent_back_to_the_agent(store, queue):
    """Wybór między merge_into i force_new wymaga porównania dwóch rekordów.
    Ekran telefonu nie jest miejscem na takie pytanie (ADR-019, ADR-021)."""
    client = FakePartDb(
        parts=[{"@id": "/api/parts/9", "name": "Czujnik HC-SR04 z podstawką"}]
    )
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])

    with pytest.raises(ProposalError, match="rozpoznanie"):
        await make_reviewer(store, queue, client).approve(proposal.id)

    assert client.created == [], "przy niejednoznaczności magazyn zostaje nietknięty"
    assert (await store.get(proposal.id)).status == "pending"


async def test_approve_item_goes_one_by_one_so_dedup_sees_the_previous_part(
    store, queue
):
    """Przy zdjęciu grupowym (#72) sąsiednie płytki bywają podobne. Plan
    policzony raz na początku nie wiedziałby o pierwszej z nich."""
    client = FakePartDb()
    item = await photo_item(queue)
    await store.create(
        item.id,
        [
            (payload(name="Przejściówka SOIC na DIP 20-pin", force_new=True), {}),
            (payload(name="Przejściówka SOIC na DIP 28-pin", force_new=True), {}),
        ],
    )

    approved, refs = await make_reviewer(store, queue, client).approve_item(item.id)

    assert len(approved) == 2
    assert client.names("parts") == [
        "Przejściówka SOIC na DIP 20-pin",
        "Przejściówka SOIC na DIP 28-pin",
    ]
    assert (await queue.get(item.id)).status == "committed"
    assert len([r for r in refs if "/api/parts/" in r]) == 2


async def test_location_hint_that_does_not_exist_still_registers_the_part(store, queue):
    """ADR-017: podpowiedź z telefonu to plan użytkownika, nie stan bazy.
    „Kuweta B3" przed jej powstaniem nie może blokować zapisu."""
    client = FakePartDb()
    item = await photo_item(queue, location_hint="Kuweta B3")
    (proposal,) = await store.create(item.id, [(payload(), {})])

    await make_reviewer(store, queue, client).approve(proposal.id)

    lots = [p for r, p in client.created if r == "part_lots"]
    assert lots and "storage_location" not in lots[0]


# ─── API dla telefonu ────────────────────────────────────────────────────────


@pytest.fixture
def web(tmp_path):
    database = Database(tmp_path / "bom.db")
    database.migrate()
    queue = IntakeQueue(database, tmp_path / "photos")
    client = FakePartDb()
    app = create_app(
        load_settings({"PARTDB_API_TOKEN": "tcp_test"}), client=client, queue=queue
    )
    with TestClient(app) as tc:
        tc.partdb = client
        tc.queue = queue
        tc.store = ProposalStore(database)
        yield tc


def upload(web):
    response = web.post(
        "/api/intake/photo",
        files={"photo": ("k.jpg", JPEG, "image/jpeg")},
        data={"location_hint": "A/A1"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def seed(web, item_id, entries=None):
    return await web.store.create(item_id, entries or [(payload(), {})])


def test_recognize_button_marks_and_unmarks(web):
    item_id = upload(web)

    marked = web.post(f"/api/intake/{item_id}/recognize")
    assert marked.status_code == 200
    assert marked.json()["recognize_requested_at"]

    cleared = web.post(f"/api/intake/{item_id}/recognize", params={"wanted": False})
    assert cleared.json()["recognize_requested_at"] is None


async def test_queue_listing_says_how_many_proposals_wait(web):
    item_id = upload(web)
    await seed(web, item_id, [(payload(), {}), (payload(name="DHT22"), {})])

    data = web.get("/api/intake").json()

    row = next(item for item in data["items"] if item["id"] == item_id)
    assert row["proposals_pending"] == 2
    assert data["counts"]["awaiting_review"] == 1
    assert row["status"] == "pending", "propozycje nie zmieniają stanu pozycji"


async def test_proposal_list_carries_the_crop_thumbnail(web):
    item_id = upload(web)
    await seed(
        web,
        item_id,
        [
            (
                payload(crop=[0, 0, 0.5, 1]),
                {"photo_url": f"/api/intake/{item_id}/photo?crop=0,0,0.5,1"},
            )
        ],
    )

    cards = web.get("/api/proposals").json()["proposals"]

    assert len(cards) == 1
    assert cards[0]["photo_url"].endswith("crop=0,0,0.5,1")


async def test_phone_can_fix_the_amount_then_approve(web):
    item_id = upload(web)
    (proposal,) = await seed(web, item_id)

    edited = web.patch(f"/api/proposals/{proposal.id}", json={"amount": 7})
    assert edited.status_code == 200
    assert edited.json()["payload"]["amount"] == 7

    approved = web.post(f"/api/proposals/{proposal.id}/approve")
    assert approved.status_code == 200
    lots = [p for r, p in web.partdb.created if r == "part_lots"]
    assert lots[0]["amount"] == 7


async def test_phone_cannot_touch_recognition_fields(web):
    item_id = upload(web)
    (proposal,) = await seed(web, item_id)

    response = web.patch(f"/api/proposals/{proposal.id}", json={"shot": 3})

    assert response.status_code == 409
    assert "shot" in response.json()["error"]


async def test_approve_all_closes_the_item(web):
    item_id = upload(web)
    await seed(
        web,
        item_id,
        [(payload(force_new=True), {}), (payload(name="DHT22", force_new=True), {})],
    )

    response = web.post(f"/api/intake/{item_id}/approve")

    assert response.status_code == 200
    assert len(response.json()["approved"]) == 2
    assert web.get("/api/intake").json()["items"][0]["status"] == "committed"


async def test_rejecting_the_only_proposal_leaves_nothing_in_partdb(web):
    item_id = upload(web)
    (proposal,) = await seed(web, item_id)

    response = web.post(
        f"/api/proposals/{proposal.id}/reject", data={"note": "nie ta część"}
    )

    assert response.status_code == 200
    assert web.partdb.created == []


# ─── Narzędzia MCP ───────────────────────────────────────────────────────────


@pytest.fixture
def mcp_env(monkeypatch, tmp_path):
    database = Database(tmp_path / "bom.db")
    database.migrate()
    queue = IntakeQueue(database, tmp_path / "photos")
    store = ProposalStore(database)
    client = FakePartDb()
    monkeypatch.setattr(mcp_server, "get_queue", lambda: queue)
    monkeypatch.setattr(mcp_server, "get_proposals", lambda: store)
    monkeypatch.setattr(mcp_server, "get_client", lambda: client)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings())
    return queue, store, client


async def call(name, arguments):
    result = await mcp_server.mcp.call_tool(name, arguments)
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


async def test_intake_propose_is_registered_and_says_it_writes_nothing():
    tools = await mcp_server.mcp.list_tools()
    tool = next((t for t in tools if t.name == "intake_propose"), None)

    assert tool is not None
    assert "magazyn" in (tool.description or "").lower()


async def test_propose_writes_proposals_not_parts(mcp_env):
    queue, store, client = mcp_env
    item = await queue.add_photo(JPEG, location_hint="A/A1")

    text = await call("intake_propose", {"item_id": item.id, "parts": [payload()]})

    assert "propozycje" in text
    assert client.created == [], "magazyn ma zostać nietknięty"
    assert len(await store.list(item_id=item.id)) == 1


async def test_propose_refuses_a_second_batch_for_the_same_item(mcp_env):
    queue, store, _ = mcp_env
    item = await queue.add_photo(JPEG)
    await store.create(item.id, [(payload(), {})])

    text = await call("intake_propose", {"item_id": item.id, "parts": [payload()]})

    assert "już" in text
    assert len(await store.list(item_id=item.id)) == 1


async def test_propose_refuses_when_dedup_is_unresolved(mcp_env):
    """Rozstrzygnięcie należy do agenta — użytkownik nie zrobi tego palcem."""
    queue, store, client = mcp_env
    client.parts.append({"@id": "/api/parts/9", "name": "Czujnik HC-SR04 na płytce"})
    item = await queue.add_photo(JPEG)

    text = await call("intake_propose", {"item_id": item.id, "parts": [payload()]})

    assert "merge_into" in text
    assert await store.list(item_id=item.id) == []


async def test_pending_shows_only_requested_items_when_asked(mcp_env):
    queue, _, _ = mcp_env
    await queue.add_photo(JPEG)
    wanted = await queue.add_photo(JPEG)
    await queue.request_recognition(wanted.id)

    text = await call("intake_pending", {"only_requested": True})

    assert f"pozycja #{wanted.id}" in text
    assert "Zleconych do rozpoznania: 1" in text


async def test_pending_skips_items_that_already_wait_for_a_human(mcp_env):
    queue, store, _ = mcp_env
    item = await queue.add_photo(JPEG)
    await store.create(item.id, [(payload(), {})])

    text = await call("intake_pending", {})

    assert "czekają na zatwierdzenie" in text
    assert f"pozycja #{item.id}" not in text


# ─── Regresja: podwójne ✓ na telefonie ───────────────────────────────────────


async def test_double_tap_on_approve_creates_one_stock_entry(store, queue):
    """Zmierzone na żywej instancji: dwa żądania zatwierdzenia przeszły oba
    sprawdzenie „czy jeszcze pending" i część dostała DWA stany po 1 szt.
    Palec na telefonie potrafi kliknąć dwa razy — magazyn nie może na tym
    tracić prawdy o ilości."""
    import asyncio

    client = FakePartDb()
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])
    reviewer = make_reviewer(store, queue, client)

    results = await asyncio.gather(
        reviewer.approve(proposal.id),
        reviewer.approve(proposal.id),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 1, "drugie kliknięcie musi odpaść"
    assert isinstance(failures[0], ProposalError)
    lots = [p for r, p in client.created if r == "part_lots"]
    assert len(lots) == 1, f"powstało {len(lots)} stanów zamiast jednego"
    assert client.names("parts") == ["HC-SR04"]


async def test_failed_write_puts_the_proposal_back_in_play(store, queue):
    """Propozycja zabrana do zapisu, który padł, musi wrócić do kolejki —
    inaczej zostałaby zabrana na zawsze i nie dałoby się jej ponowić."""

    class Broken(FakePartDb):
        async def create_part(self, payload):
            raise RuntimeError("Part-DB padło w połowie")

    client = Broken()
    item = await photo_item(queue)
    (proposal,) = await store.create(item.id, [(payload(), {})])
    reviewer = make_reviewer(store, queue, client)

    with pytest.raises(RuntimeError):
        await reviewer.approve(proposal.id)

    assert (await store.get(proposal.id)).status == "pending"
    # Drugie podejście musi mieć co zabrać.
    assert await store.claim(proposal.id) is True


# ─── Twarda blokada dla sesji bez nadzoru (#75) ───────────────────────────────


async def test_locked_session_ignores_a_request_for_the_whole_queue(
    mcp_env, monkeypatch
):
    """`only_requested` w argumencie to prośba, którą model może pominąć.
    Przy INTAKE_ONLY_REQUESTED=1 zawężenie jest właściwością narzędzia:
    nieoznaczone zdjęcie nie istnieje dla tej sesji."""
    queue, _, _ = mcp_env
    monkeypatch.setattr(
        mcp_server, "get_settings", lambda: Settings(intake_only_requested=True)
    )
    ignored = await queue.add_photo(JPEG)
    wanted = await queue.add_photo(JPEG)
    await queue.request_recognition(wanted.id)

    # Model prosi WPROST o całą kolejkę — narzędzie ma go nie posłuchać.
    text = await call("intake_pending", {"only_requested": False, "limit": 20})

    assert f"pozycja #{wanted.id}" in text
    assert f"pozycja #{ignored.id}" not in text
    assert "WYŁĄCZNIE pozycje zlecone" in text


async def test_unlocked_session_still_sees_the_whole_queue(mcp_env):
    """Moja zwykła sesja musi widzieć wszystko — inaczej nie dałoby się
    wczytać kartonu za jednym razem."""
    queue, _, _ = mcp_env
    a = await queue.add_photo(JPEG)
    b = await queue.add_photo(JPEG)

    text = await call("intake_pending", {"limit": 20})

    assert f"pozycja #{a.id}" in text and f"pozycja #{b.id}" in text
    assert "WYŁĄCZNIE" not in text


def test_cron_config_pins_the_lock_and_drops_partdb():
    """Zmienna musi jechać przez `docker exec -e`: eksport w skrypcie
    z crona nie dotarłby do procesu w kontenerze."""
    import json
    from pathlib import Path

    config = json.loads(
        (
            Path(__file__).resolve().parent.parent / "scripts" / "mcp-cron.json"
        ).read_text(encoding="utf-8")
    )
    args = config["mcpServers"]["bom"]["args"]
    assert "-e" in args and "INTAKE_ONLY_REQUESTED=1" in args
    assert "partdb" not in config["mcpServers"], (
        "automat nie ma po co dotykać magazynu, nawet do odczytu"
    )


def test_cron_script_uses_the_strict_config_and_three_tools():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "recognize_requested.sh"
    ).read_text(encoding="utf-8")
    assert "--strict-mcp-config" in script
    assert "scripts/mcp-cron.json" in script

    allowlist = next(line for line in script.splitlines() if "--allowed-tools" in line)
    for tool in ("intake_pending", "intake_zoom", "intake_propose"):
        assert f"mcp__bom__{tool}" in allowlist
    # Narzędzia zapisujące do magazynu nie mogą się tu znaleźć — komentarz
    # wyżej w skrypcie wyjaśnia ich brak, więc szukamy w samej liście.
    for forbidden in ("intake_commit", "intake_reject", "assign_location"):
        assert forbidden not in allowlist


async def test_pending_lists_existing_categories_for_the_agent(mcp_env):
    """Sesja bez nadzoru nie widzi Part-DB (ma trzy narzędzia i żadnego do
    odczytu magazynu), więc bez tej listy wymyśla kategorie od zera —
    zmierzone: „moduł GPS" obok istniejących „Moduły komunikacyjne"."""
    queue, _, _ = mcp_env
    await queue.add_photo(JPEG)

    text = await call("intake_pending", {})

    assert "Kategorie w bazie" in text
    assert "Czujniki" in text


async def test_missing_categories_do_not_stop_recognition(mcp_env, monkeypatch):
    """Awaria Part-DB nie może wstrzymać rozpoznawania — plan i tak pokaże,
    że kategoria jest nowa."""
    queue, _, client = mcp_env
    await queue.add_photo(JPEG)

    async def broken():
        from bom_agent.partdb_client import PartDbError

        raise PartDbError("Part-DB nie odpowiada")

    monkeypatch.setattr(client, "list_categories", broken)

    text = await call("intake_pending", {})

    assert "nie udało się odczytać listy kategorii" in text.lower()
    assert "pozycja #" in text


async def test_one_broken_photo_does_not_break_the_whole_listing(mcp_env, tmp_path):
    """Zmierzone na instancji: jeden uszkodzony plik (ucięta wysyłka, artefakt
    testu) wywracał całe `intake_pending` i kolejka stawała się nieczytelna —
    także dla automatu, który wtedy nie robił nic i nie mówił dlaczego."""
    queue, _, _ = mcp_env
    broken = await queue.add_photo(b"\xff\xd8\xff\xe0 to nie jest obraz")
    healthy = await queue.add_photo(JPEG)

    text = await call("intake_pending", {"limit": 5})

    assert f"pozycja #{healthy.id}" in text, "zdrowe zdjęcie musi się pokazać"
    assert f"pozycja #{broken.id}" in text
    assert "nie da się odczytać" in text
    assert "powtórzenie zdjęcia" in text


# ─── Regresja: zakleszczenie okna zleceń (7 godzin bez postępu) ──────────────


async def test_items_awaiting_review_do_not_block_the_recognition_window(store, queue):
    """Zmierzone na instancji: 19 zleconych, 8 z propozycją, 11 do wzięcia —
    a automat przez SIEDEM GODZIN meldował „brak zleceń". `LIMIT` szedł przed
    filtrem „ma już propozycję", więc okno najstarszych zleceń wypełniały
    pozycje czekające na klik człowieka, a te do rozpoznania nie wchodziły
    do niego nigdy. Zakleszczenie odblokowywał dopiero człowiek — czyli ten,
    kogo automat miał wyręczyć."""
    proposed = []
    for _ in range(8):
        item = await photo_item(queue)
        await queue.request_recognition(item.id)
        await store.create(item.id, [(payload(), {})])
        proposed.append(item.id)

    fresh = []
    for _ in range(3):
        item = await photo_item(queue)
        await queue.request_recognition(item.id)
        fresh.append(item.id)

    # Okno wielkości ośmiu — dokładnie tyle, ile pozycji z propozycjami.
    window = await queue.requested(limit=8)

    ids = [item.id for item in window]
    assert ids, "okno nie może być puste, gdy trzy pozycje czekają na rozpoznanie"
    assert set(ids) <= set(fresh)
    assert not set(ids) & set(proposed), (
        "pozycja z propozycją do zatwierdzenia nie jest pracą dla automatu"
    )


async def test_recognition_window_is_oldest_first_among_real_work(store, queue):
    """Kolejność ma zostać „najstarsze pierwsze", ale liczona po odsianiu
    tego, co czeka na człowieka."""
    first = await photo_item(queue)
    await queue.request_recognition(first.id)
    second = await photo_item(queue)
    await queue.request_recognition(second.id)
    await store.create(first.id, [(payload(), {})])

    window = await queue.requested(limit=1)

    assert [item.id for item in window] == [second.id]
