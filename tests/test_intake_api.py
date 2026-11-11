"""API kolejki i serwowanie PWA (#20, #21)."""

import pytest
from fastapi.testclient import TestClient

from bom_agent.config import load_settings
from bom_agent.db import Database
from bom_agent.intake.queue import IntakeQueue
from bom_agent.partdb_client import PartDbStatus
from bom_agent.web import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"udawany jpeg" * 10


class StubPartDb:
    async def ping(self) -> PartDbStatus:
        return PartDbStatus(reachable=True, authorized=True, total_parts=25)

    async def aclose(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def client(tmp_path):
    db = Database(tmp_path / "bom.db")
    db.migrate()
    queue = IntakeQueue(db, tmp_path / "photos")
    app = create_app(
        load_settings({"PARTDB_API_TOKEN": "tcp_test"}),
        client=StubPartDb(),
        queue=queue,
    )
    with TestClient(app) as tc:
        yield tc


def test_photo_upload_returns_the_queued_item(client):
    response = client.post(
        "/api/intake/photo",
        files={"photo": ("kuweta.jpg", JPEG, "image/jpeg")},
        data={"location_hint": "A/A1"},
    )
    assert response.status_code == 201
    item = response.json()
    assert item["status"] == "pending"
    assert item["kind"] == "photo"
    assert item["location_hint"] == "A/A1"
    assert item["photo_url"] == f"/api/intake/{item['id']}/photo"


def test_uploaded_photo_can_be_previewed(client):
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()
    preview = client.get(item["photo_url"])
    assert preview.status_code == 200
    assert preview.content == JPEG
    assert preview.headers["content-type"].startswith("image/")


def test_upload_without_location_is_allowed(client):
    """Lepiej zdjęcie bez lokalizacji niż brak zdjęcia — hint uzupełni agent."""
    response = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    )
    assert response.status_code == 201
    assert response.json()["location_hint"] is None


def test_untyped_gallery_file_is_accepted_by_extension(client):
    """Wybieraki na Androidzie bywają niedbałe i podają `octet-stream`
    zamiast typu obrazu. Odrzucenie zdjęcia z tego powodu byłoby karaniem
    użytkownika za cudzy błąd."""
    for filename, sent_type in (
        ("IMG_20260814.jpg", "application/octet-stream"),
        ("zrzut.png", ""),
        ("foto.jpeg", "application/octet-stream"),
    ):
        response = client.post(
            "/api/intake/photo", files={"photo": (filename, JPEG, sent_type)}
        )
        assert response.status_code == 201, (filename, response.text)


def test_untyped_non_image_is_still_refused(client):
    response = client.post(
        "/api/intake/photo",
        files={"photo": ("notatki.txt", b"cokolwiek", "application/octet-stream")},
    )
    assert response.status_code == 415
    assert "notatki.txt" in response.json()["detail"]


def test_non_image_upload_is_refused(client):
    response = client.post(
        "/api/intake/photo",
        files={"photo": ("zlosliwy.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 415
    assert "application/pdf" in response.json()["detail"]


def test_empty_photo_is_a_client_error_not_a_crash(client):
    response = client.post(
        "/api/intake/photo", files={"photo": ("pusty.jpg", b"", "image/jpeg")}
    )
    assert response.status_code == 400
    assert "Puste zdjęcie" in response.json()["error"]


def test_dictation_goes_to_the_queue_verbatim(client):
    text = "kuweta B3, dużo 100nF 0805"
    response = client.post(
        "/api/intake/voice", data={"text": text, "location_hint": "B/B3"}
    )
    assert response.status_code == 201
    assert response.json()["payload_text"] == text
    assert response.json()["kind"] == "voice"


def test_typed_description_is_requested_for_recognition_on_send(client):
    """Wysłany opis jest od razu zleceniem (#75).

    Powód, dla którego zdjęcie czeka na „Rozpoznaj" — dokładane ujęcie
    spodu (#66) — nie dotyczy tekstu: lista wpisana palcem jest kompletna
    w chwili wysłania, a wiersz tekstowy nie ma nawet przycisku zlecenia.
    """
    text_item = client.post("/api/intake/voice", data={"text": "BC557 10 szt"}).json()
    photo = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()

    assert text_item["recognize_requested_at"]
    assert not photo["recognize_requested_at"]


def test_photo_note_is_saved_cleared_and_refused_for_text_items(client):
    """Notatka do zdjęcia (#75): ilość i nazwa, których nie widać na obrazie."""
    photo = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()

    saved = client.post(f"/api/intake/{photo['id']}/note", data={"text": " BC557, 10 szt "})
    assert saved.status_code == 200
    assert saved.json()["payload_text"] == "BC557, 10 szt"

    cleared = client.post(f"/api/intake/{photo['id']}/note", data={"text": ""})
    assert cleared.json()["payload_text"] is None

    # Opis tekstowy JEST treścią — notatka nadpisałaby to, co wpisał użytkownik.
    text_item = client.post("/api/intake/voice", data={"text": "BC547 20 szt"}).json()
    refused = client.post(f"/api/intake/{text_item['id']}/note", data={"text": "x"})
    assert refused.status_code == 400


def test_location_hint_is_set_and_cleared_for_any_pending_item(client):
    """Pudełko wskazane przed rozpoznaniem (#77) — dla zdjęcia i dla tekstu."""
    photo = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()
    saved = client.post(f"/api/intake/{photo['id']}/location", data={"text": " A/A1 "})
    assert saved.status_code == 200
    assert saved.json()["location_hint"] == "A/A1"

    cleared = client.post(f"/api/intake/{photo['id']}/location", data={"text": ""})
    assert cleared.json()["location_hint"] is None

    text_item = client.post("/api/intake/voice", data={"text": "BC547 20 szt"}).json()
    ok = client.post(f"/api/intake/{text_item['id']}/location", data={"text": "B/B2"})
    assert ok.json()["location_hint"] == "B/B2"


def test_listing_reports_items_and_counts(client):
    client.post("/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")})
    client.post("/api/intake/voice", data={"text": "opis"})

    body = client.get("/api/intake").json()
    assert [i["kind"] for i in body["items"]] == ["voice", "photo"]
    assert body["counts"]["pending"] == 2


def test_rejection_requires_a_note_and_records_it(client):
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()

    assert client.post(f"/api/intake/{item['id']}/reject").status_code == 422
    rejected = client.post(
        f"/api/intake/{item['id']}/reject", data={"note": "rozmazane"}
    ).json()
    assert rejected["status"] == "rejected"
    assert rejected["note"] == "rozmazane"


def test_missing_item_is_reported(client):
    assert client.get("/api/intake/9999/photo").status_code == 400


def test_photo_can_be_deleted_from_the_app(client):
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()

    response = client.delete(f"/api/intake/{item['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": item["id"], "kind": "photo"}

    assert client.get("/api/intake").json()["items"] == []
    assert client.get(item["photo_url"]).status_code == 400


def test_committed_item_cannot_be_deleted_through_the_api(client, tmp_path):
    item = client.post(
        "/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")}
    ).json()
    # Zatwierdzenie robi agent; tu wystarczy sam stan.
    import asyncio

    from bom_agent.db import Database
    from bom_agent.intake.queue import IntakeQueue

    queue = IntakeQueue(Database(tmp_path / "bom.db"), tmp_path / "photos")
    asyncio.run(queue.commit(item["id"], partdb_refs=["/api/parts/1"]))

    response = client.delete(f"/api/intake/{item['id']}")
    assert response.status_code == 400
    assert "zatwierdzona" in response.json()["error"]


def test_health_now_reports_queue_state(client):
    client.post("/api/intake/photo", files={"photo": ("k.jpg", JPEG, "image/jpeg")})
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["intake"]["pending"] == 1


def test_pwa_is_served_and_does_not_shadow_the_api(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "Warsztat" in index.text

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"

    assert client.get("/sw.js").status_code == 200
    assert client.get("/icons/icon-192.png").status_code == 200
    # API nadal odpowiada, mimo zamontowania katalogu na "/"
    assert client.get("/api/intake").status_code == 200


def test_extra_shot_goes_into_the_same_queue_item(client):
    """#66: przycisk „+" w wierszu dokłada ujęcie, nie nową pozycję."""
    first = client.post(
        "/api/intake/photo",
        files={"photo": ("gora.jpg", JPEG, "image/jpeg")},
        data={"location_hint": "A/A1"},
    ).json()

    second = client.post(
        "/api/intake/photo",
        files={"photo": ("spod.jpg", JPEG, "image/jpeg")},
        data={"parent_id": str(first["id"])},
    ).json()

    assert second["id"] == first["id"]
    assert len(second["shots"]) == 2
    assert second["location_hint"] == "A/A1"

    listing = client.get("/api/intake").json()
    assert [row["id"] for row in listing["items"]] == [first["id"]]
    assert listing["counts"]["pending"] == 1

    # Każde ujęcie ma własny podgląd — inaczej telefon pokazałby jedno zdjęcie
    # w miejscu, w którym są cztery.
    for shot in second["shots"]:
        assert client.get(shot["url"]).status_code == 200


def test_shot_added_to_a_missing_item_is_a_readable_error(client):
    response = client.post(
        "/api/intake/photo",
        files={"photo": ("k.jpg", JPEG, "image/jpeg")},
        data={"parent_id": "4242"},
    )
    assert response.status_code == 400
    assert "nie istnieje" in response.json()["error"]
