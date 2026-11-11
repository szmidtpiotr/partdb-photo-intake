"""Kolejka intake: trwałość i przejścia stanów (#18, #19)."""

import pytest

from bom_agent.db import SCHEMA_VERSION, Database
from bom_agent.intake.queue import IntakeError, IntakeQueue


@pytest.fixture
def queue(tmp_path):
    db = Database(tmp_path / "bom.db")
    db.migrate()
    return IntakeQueue(db, tmp_path / "photos")


def test_migration_sets_schema_version_and_enables_wal(tmp_path):
    db = Database(tmp_path / "bom.db")
    assert db.migrate() == SCHEMA_VERSION
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"intake_item", "op_log"} <= tables


def test_migration_is_idempotent(tmp_path):
    db = Database(tmp_path / "bom.db")
    db.migrate()
    assert db.migrate() == SCHEMA_VERSION


async def test_photo_lands_on_disk_not_in_the_database(queue, tmp_path):
    item = await queue.add_photo(b"\xff\xd8\xff-udawany-jpeg", location_hint=" A/A1 ")

    assert item.status == "pending"
    assert item.kind == "photo"
    assert item.location_hint == "A/A1"
    stored = next((tmp_path / "photos").iterdir())
    assert stored.read_bytes() == b"\xff\xd8\xff-udawany-jpeg"
    assert item.as_dict()["photo_url"] == f"/api/intake/{item.id}/photo"
    # Ścieżka serwera nie wychodzi na zewnątrz.
    assert "payload_path" not in item.as_dict()


async def test_queue_survives_a_new_connection(tmp_path):
    """Restart kontenera nie może zgubić zdjęć — to jest cały sens kolejki."""
    db = Database(tmp_path / "bom.db")
    db.migrate()
    first = IntakeQueue(db, tmp_path / "photos")
    item = await first.add_photo(b"jpeg", location_hint="B/B2")

    reopened = IntakeQueue(Database(tmp_path / "bom.db"), tmp_path / "photos")
    again = await reopened.get(item.id)
    assert again.status == "pending"
    assert again.location_hint == "B/B2"


async def test_text_item_keeps_the_dictation_verbatim(queue):
    text = "kuweta B3, dużo 100nF 0805, ze dwadzieścia elektrolitów 470u"
    item = await queue.add_text(text, location_hint="B/B3")
    assert item.kind == "voice"
    assert item.payload_text == text
    assert item.as_dict().get("photo_url") is None


async def test_empty_payloads_are_rejected(queue):
    with pytest.raises(IntakeError):
        await queue.add_photo(b"")
    with pytest.raises(IntakeError):
        await queue.add_text("   ")


async def test_full_lifecycle(queue):
    item = await queue.add_photo(b"jpeg")
    assert (await queue.claim(item.id)).status == "processing"
    done = await queue.commit(
        item.id, result={"parts": ["ESP32"]}, partdb_refs=["/api/parts/1"]
    )
    assert done.status == "committed"
    assert done.result == {"parts": ["ESP32"]}
    assert done.partdb_refs == ["/api/parts/1"]
    assert done.processed_at


async def test_commit_may_skip_processing(queue):
    """Agent bywa szybszy niż ceremonia — zapis wprost z `pending` jest OK."""
    item = await queue.add_photo(b"jpeg")
    assert (await queue.commit(item.id)).status == "committed"


async def test_committed_item_cannot_be_rejected(queue):
    item = await queue.add_photo(b"jpeg")
    await queue.commit(item.id)
    with pytest.raises(IntakeError, match="committed"):
        await queue.reject(item.id, "pomyłka")


async def test_rejection_requires_a_reason(queue):
    item = await queue.add_photo(b"jpeg")
    with pytest.raises(IntakeError):
        await queue.reject(item.id, "   ")


async def test_rejected_photo_stays_on_disk(queue, tmp_path):
    """Nieostre ujęcie chce się obejrzeć przed powtórzeniem (06-intake)."""
    item = await queue.add_photo(b"jpeg")
    await queue.reject(item.id, "rozmazane")
    assert next((tmp_path / "photos").iterdir()).exists()
    assert (await queue.get(item.id)).note == "rozmazane"


async def test_repeated_transition_is_a_no_op(queue):
    item = await queue.add_photo(b"jpeg")
    await queue.claim(item.id)
    assert (await queue.claim(item.id)).status == "processing"


async def test_listing_is_newest_first_and_filterable(queue):
    first = await queue.add_photo(b"a")
    second = await queue.add_text("opis")
    await queue.commit(first.id)

    assert [i.id for i in await queue.list()] == [second.id, first.id]
    assert [i.id for i in await queue.list(status="pending")] == [second.id]
    assert [i.id for i in await queue.list(status="committed")] == [first.id]
    assert await queue.counts() == {
        "pending": 1,
        "processing": 0,
        "committed": 1,
        "rejected": 0,
    }


async def test_delete_removes_the_row_and_the_file(queue, tmp_path):
    """Nieudane ujęcie — palec przed obiektywem — znika razem z plikiem."""
    item = await queue.add_photo(b"jpeg")
    photo = next((tmp_path / "photos").iterdir())

    await queue.delete(item.id)

    assert not photo.exists()
    assert await queue.list() == []
    with pytest.raises(IntakeError, match="nie istnieje"):
        await queue.get(item.id)


async def test_rejected_item_can_still_be_deleted(queue):
    item = await queue.add_photo(b"jpeg")
    await queue.reject(item.id, "rozmazane")

    await queue.delete(item.id)
    assert await queue.counts() == {
        "pending": 0,
        "processing": 0,
        "committed": 0,
        "rejected": 0,
    }


async def test_committed_item_is_never_deleted(queue, tmp_path):
    """Zatwierdzona pozycja wskazuje na części w Part-DB i jest jedynym
    śladem pozwalającym cofnąć zapis."""
    item = await queue.add_photo(b"jpeg")
    await queue.commit(item.id, partdb_refs=["/api/parts/7"])

    with pytest.raises(IntakeError, match="zatwierdzona"):
        await queue.delete(item.id)

    assert (await queue.get(item.id)).status == "committed"
    assert next((tmp_path / "photos").iterdir()).exists()


async def test_deleting_a_text_item_needs_no_file(queue):
    item = await queue.add_text("kuweta B3, dużo 100nF")
    await queue.delete(item.id)
    assert await queue.list() == []


async def test_delete_survives_a_missing_file(queue, tmp_path):
    """Plik mógł zniknąć przy sprzątaniu dysku — wpis i tak ma dać się usunąć."""
    item = await queue.add_photo(b"jpeg")
    next((tmp_path / "photos").iterdir()).unlink()

    await queue.delete(item.id)
    assert await queue.list() == []


async def test_unknown_item_and_status_are_reported(queue):
    with pytest.raises(IntakeError, match="nie istnieje"):
        await queue.get(4242)
    with pytest.raises(IntakeError, match="Nieznany stan"):
        await queue.list(status="wymyslony")


# --- kilka ujęć jednej rzeczy (#66) ----------------------------------------


async def test_extra_shot_joins_the_item_instead_of_making_a_new_one(queue):
    """Moduł sfotografowany z wierzchu i od spodu to jeden element.
    Osobne pozycje oznaczałyby dwa zapisy i podwójny stan w magazynie."""
    first = await queue.add_photo(b"jpeg-gora", location_hint="A/A1")
    item = await queue.add_photo(b"jpeg-spod", parent_id=first.id)

    # Odpowiedzią jest pozycja główna — ujęcie samo w sobie niczym nie jest.
    assert item.id == first.id
    assert [shot.id for shot in item.shots] == [first.id, first.id + 1]
    assert item.as_dict()["shots"] == [
        {"id": first.id, "url": f"/api/intake/{first.id}/photo"},
        {"id": first.id + 1, "url": f"/api/intake/{first.id + 1}/photo"},
    ]

    # Kolejka pokazuje jedną rzecz do rozpoznania, nie dwie.
    assert [row.id for row in await queue.list()] == [first.id]
    assert (await queue.counts())["pending"] == 1


async def test_extra_shot_inherits_the_location_of_its_item(queue):
    first = await queue.add_photo(b"jpeg", location_hint="B/B2")
    await queue.add_photo(b"jpeg-2", parent_id=first.id, location_hint="Z/Z9")

    shots = (await queue.get(first.id)).shots
    child = await queue.get(shots[1].id)
    assert child.location_hint == "B/B2"


async def test_shot_added_to_a_shot_lands_on_the_main_item(queue):
    first = await queue.add_photo(b"jpeg")
    second = (await queue.add_photo(b"jpeg-2", parent_id=first.id)).shots[1]

    item = await queue.add_photo(b"jpeg-3", parent_id=second.id)
    assert item.id == first.id
    assert len(item.shots) == 3


async def test_committing_an_item_closes_its_shots(queue):
    """Ujęcie zostawione w `pending` wróciłoby do rozpoznania jako osobna
    rzecz — czyli dokładnie ten błąd, który grupowanie usuwa."""
    first = await queue.add_photo(b"jpeg")
    item = await queue.add_photo(b"jpeg-2", parent_id=first.id)
    shot_id = item.shots[1].id

    await queue.commit(first.id, partdb_refs=["/api/parts/7"])

    assert (await queue.get(shot_id)).status == "committed"
    assert (await queue.counts())["committed"] == 1
    assert await queue.list(status="pending") == []


async def test_deleting_an_item_takes_all_of_its_shots(queue, tmp_path):
    first = await queue.add_photo(b"jpeg")
    await queue.add_photo(b"jpeg-2", parent_id=first.id)

    await queue.delete(first.id)

    assert await queue.list() == []
    assert list((tmp_path / "photos").iterdir()) == []


async def test_a_single_bad_shot_can_be_deleted_alone(queue, tmp_path):
    """Poruszone ujęcie kasuje się osobno — pozycja zostaje."""
    first = await queue.add_photo(b"jpeg")
    shot = (await queue.add_photo(b"jpeg-poruszone", parent_id=first.id)).shots[1]

    await queue.delete(shot.id)

    item = await queue.get(first.id)
    assert [s.id for s in item.shots] == [first.id]
    assert len(list((tmp_path / "photos").iterdir())) == 1


async def test_shots_go_only_to_a_photo_still_waiting(queue):
    dictated = await queue.add_text("kuweta B3, dużo 100nF")
    with pytest.raises(IntakeError, match="nie zdjęcie"):
        await queue.add_photo(b"jpeg", parent_id=dictated.id)

    done = await queue.add_photo(b"jpeg")
    await queue.commit(done.id, partdb_refs=["/api/parts/7"])
    with pytest.raises(IntakeError, match="czekaj"):
        await queue.add_photo(b"jpeg-2", parent_id=done.id)


async def test_item_with_rejected_proposals_can_be_deleted(queue):
    """„Wyrzuć tę rzecz” po odrzuconej propozycji (#77): klucz obcy
    propozycji nie może zablokować skasowania, a plik ma zostać, gdy baza
    odmówi."""
    from bom_agent.intake.proposals import ProposalStore

    store = ProposalStore(queue.db)
    item = await queue.add_photo(b"jpeg")
    (proposal,) = await store.create(item.id, [({"name": "X", "amount": 1}, {})])
    await store.decide(proposal.id, "rejected")

    await queue.delete(item.id)
    assert await queue.list() == []


async def test_item_with_an_approved_proposal_is_kept(queue, tmp_path):
    from bom_agent.intake.proposals import ProposalStore

    store = ProposalStore(queue.db)
    item = await queue.add_photo(b"jpeg")
    (proposal,) = await store.create(item.id, [({"name": "X", "amount": 1}, {})])
    await store.decide(proposal.id, "approved")

    with pytest.raises(IntakeError, match="zapisano już części"):
        await queue.delete(item.id)
    assert next((tmp_path / "photos").iterdir()).exists()
