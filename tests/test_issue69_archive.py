"""TDD: Issue #69 — PWA rozdziela kolejkę na „Czeka" i „Archiwum".

Backend: pozycja zatwierdzona ma nieść **nazwę** powstałej części i odnośnik
do jej rekordu w Part-DB, nie surowe `/api/parts/131`. Front: dwa widoki
przełączane bez przeładowania, archiwum czytane z serwera (przeżywa
zamknięcie karty).
"""

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bom_agent.config import Settings, load_settings
from bom_agent.db import Database
from bom_agent.intake.queue import IntakeItem, IntakeQueue
from bom_agent.partdb_client import PartDbStatus
from bom_agent.web import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"udawany jpeg" * 10
PWA = Path(__file__).resolve().parent.parent / "pwa"
INDEX = (PWA / "index.html").read_text(encoding="utf-8")
APP = (PWA / "app.js").read_text(encoding="utf-8")


class StubPartDb:
    async def ping(self) -> PartDbStatus:
        return PartDbStatus(reachable=True, authorized=True, total_parts=25)

    async def aclose(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "bom.db")
    db.migrate()
    queue = IntakeQueue(db, tmp_path / "photos")
    app = create_app(
        load_settings(
            {
                "PARTDB_API_TOKEN": "tcp_test",
                "PARTDB_PUBLIC_URL": "https://partdb.example.test",
            }
        ),
        client=StubPartDb(),
        queue=queue,
    )
    with TestClient(app) as tc:
        yield tc, queue


# ─── Nazwa i link zamiast surowego IRI ───────────────────────────────────────


def test_part_records_pair_names_with_partdb_ids():
    """Nazwa jest w `result`, numer rekordu — w `partdb_refs` wśród kategorii
    i stanów. `part_records` wyławia `/api/parts/N` i łączy go z nazwą."""
    item = IntakeItem(
        id=1,
        kind="photo",
        status="committed",
        created_at="2026-08-15T10:00:00+00:00",
        result=[{"name": "HC-SR04", "amount": 5}],
        partdb_refs=["/api/categories/3", "/api/parts/42", "/api/part_lots/9"],
    )
    assert item.part_records() == [{"name": "HC-SR04", "part_id": 42}]


def test_part_records_omit_id_when_a_part_was_merged():
    """Pozycja scalona w istniejącą część nie ma własnego `/api/parts/N`.
    Gdy liczba nowych części nie zgadza się z liczbą nazw, numeru nie
    zgadujemy po kolejności — inaczej link wskazałby złą część."""
    item = IntakeItem(
        id=2,
        kind="photo",
        status="committed",
        created_at="2026-08-15T10:00:00+00:00",
        result=[{"name": "TP4056"}, {"name": "MAX7219"}],
        partdb_refs=["/api/parts/50", "/api/part_lots/9", "/api/part_lots/10"],
    )
    assert item.part_records() == [
        {"name": "TP4056", "part_id": None},
        {"name": "MAX7219", "part_id": None},
    ]


def test_partdb_part_url_builds_a_record_link():
    settings = Settings(partdb_public_url="https://partdb.example.test")
    assert settings.partdb_part_url(42) == "https://partdb.example.test/en/part/42"


def test_committed_items_carry_part_name_and_link(env):
    """`/api/intake?status=committed` oddaje nazwę i klikalny link, nie IRI."""
    client, queue = env
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()
    asyncio.run(
        queue.commit(
            item["id"],
            result=[{"name": "HC-SR04", "amount": 5}],
            partdb_refs=["/api/categories/3", "/api/parts/42", "/api/part_lots/9"],
        )
    )

    archive = client.get("/api/intake?status=committed").json()
    records = archive["items"][0]["created_parts"]
    assert records[0]["name"] == "HC-SR04"
    assert records[0]["url"] == "https://partdb.example.test/en/part/42"


def test_rejected_items_are_in_the_archive_with_their_reason(env):
    client, _ = env
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()
    client.post(f"/api/intake/{item['id']}/reject", data={"note": "rozmazane"})

    archive = client.get("/api/intake?status=rejected").json()
    assert archive["items"][0]["note"] == "rozmazane"


# ─── Front: dwa widoki ───────────────────────────────────────────────────────


def test_index_has_queue_and_archive_tabs():
    assert 'id="tab-queue"' in INDEX
    assert 'id="tab-archive"' in INDEX
    assert "Czeka" in INDEX
    assert "Archiwum" in INDEX


def test_app_routes_committed_and_rejected_into_the_archive():
    """Widok archiwum grupuje `committed` i `rejected`, kolejka — resztę."""
    assert "created_parts" in APP
    assert re.search(r"committed", APP)
    assert re.search(r"rejected", APP)
    # Przełącznik widoku bez przeładowania — jawny stan aktywnej zakładki.
    assert "activeTab" in APP
