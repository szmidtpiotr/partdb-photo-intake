"""Plan i zapis rozpoznanych części do Part-DB (#23)."""

import pytest

from bom_agent.intake.commit import (
    CommitError,
    IntakeCommitter,
    PartSpec,
    SourcePhoto,
    normalize,
    tokens,
)
from bom_agent.partdb_client import PartDbValidationError

LOCATIONS = [
    {
        "@id": "/api/storage_locations/2",
        "name": "Kuweta A1",
        "full_path": "Karton A → Kuweta A1",
    },
    {
        "@id": "/api/storage_locations/5",
        "name": "Kuweta B1",
        "full_path": "Karton B → Kuweta B1",
    },
]

CATEGORIES = [
    {"@id": "/api/categories/2", "name": "Mikrokontrolery i płytki"},
    {"@id": "/api/categories/3", "name": "Czujniki"},
]

PARTS = [
    {
        "@id": "/api/parts/1",
        "name": "ESP32-WROOM-32 DevKit v1",
        "manufacturer_product_number": "ESP32-DevKitC",
    },
]

ATTACHMENT_TYPES = [
    {"@id": "/api/attachment_types/1", "name": "Datasheet"},
    {"@id": "/api/attachment_types/2", "name": "Zdjęcie"},
]

PHOTO = SourcePhoto(
    url="https://intake.example.com/api/intake/17/photo", item_id=17
)


class FakeClient:
    """Podstawka pod `PartDbClient` — zapisuje, co dostała."""

    def __init__(self, parts=None, fail_on=None, attachment_types=None):
        self.parts = list(parts if parts is not None else PARTS)
        self.attachment_types = (
            ATTACHMENT_TYPES if attachment_types is None else list(attachment_types)
        )
        self.created: list[tuple[str, dict]] = []
        self.patched: list[tuple[str, dict]] = []
        self._next_id = 100
        self._fail_on = fail_on

    async def list_storage_locations(self):
        return LOCATIONS

    async def list_categories(self):
        return CATEGORIES

    async def list_parts(self):
        return self.parts

    async def list_attachment_types(self):
        return self.attachment_types

    async def create_attachment(self, payload):
        return await self.create("attachments", payload)

    async def create(self, resource, payload):
        if self._fail_on == resource:
            raise AssertionError(f"nie powinno tworzyć {resource}")
        self._next_id += 1
        iri = f"/api/{resource}/{self._next_id}"
        self.created.append((resource, payload))
        return {"@id": iri, **payload}

    async def create_part(self, payload):
        return await self.create("parts", payload)

    async def create_part_lot(self, payload):
        return await self.create("part_lots", payload)

    async def patch(self, iri, payload):
        self.patched.append((iri, payload))
        return {"@id": iri, **payload}


def spec(**kwargs):
    return PartSpec.from_dict({"name": "HC-SR04", "amount": 5, **kwargs})


def test_normalize_ignores_case_and_punctuation():
    assert normalize("ESP32-WROOM-32 DevKit v1") == normalize(
        "esp32 wroom 32 devkit v1"
    )
    assert normalize("") == ""


async def test_location_resolves_by_code_name_and_full_path():
    committer = IntakeCommitter(FakeClient())

    for wanted in ("A/A1", "Kuweta A1", "kuweta a1", "Karton A → Kuweta A1"):
        assert (await committer.resolve_location(wanted))["@id"] == (
            "/api/storage_locations/2"
        )


async def test_unknown_location_lists_the_known_ones():
    """Pojemnika nie wymyślamy — części „leżałyby" tam, gdzie ich nie ma."""
    committer = IntakeCommitter(FakeClient())

    with pytest.raises(CommitError, match="A/A1"):
        await committer.resolve_location("Z/Z9")


async def test_hint_from_dictation_before_any_box_exists_points_to_adr_017():
    """Na początku pudeł nie ma wcale (ADR-017), a dyktowanie i tak mówi
    „kuweta B3" — komunikat ma prowadzić do zapisu bez lokalizacji, a nie
    pokazywać pustą listę dostępnych pojemników."""

    class NoLocations(FakeClient):
        async def list_storage_locations(self):
            return []

    with pytest.raises(CommitError, match="ADR-017"):
        await IntakeCommitter(NoLocations()).resolve_location("Kuweta B3")


async def test_empty_location_means_not_assigned_yet():
    """ADR-017: „mam, ale nie wiem gdzie" to stan, nie brak danych."""
    assert await IntakeCommitter(FakeClient()).resolve_location("  ") is None


async def test_unassigned_plan_says_so_and_creates_a_lot_without_location():
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Czujniki")], location="")
    assert "BEZ PRZYPISANIA" in plan.as_text()

    await committer.execute(plan)
    lot = dict(client.created)["part_lots"]
    assert "storage_location" not in lot
    assert lot["amount"] == 5


async def test_plan_changes_nothing():
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Czujniki")], "A/A1")

    assert client.created == []
    assert "Nic jeszcze nie zapisano" in plan.as_text()
    assert "HC-SR04" in plan.as_text()


async def test_existing_part_gets_a_new_lot_not_a_duplicate():
    """Ten sam moduł w drugim pojemniku to nowy PartLot, nie druga część."""
    client = FakeClient()
    committer = IntakeCommitter(client)
    duplicate = PartSpec.from_dict(
        {"name": "esp32 wroom 32 devkit v1", "amount": 2, "category": "Cokolwiek"}
    )

    plan = await committer.plan([duplicate], "A/A1")
    assert plan.parts[0].existing_part["@id"] == "/api/parts/1"
    assert "dołoży stan" in plan.parts[0].describe()

    refs = await committer.execute(plan)
    resources = [resource for resource, _ in client.created]
    assert resources == ["part_lots"]
    assert refs == ["/api/part_lots/101"]


async def test_same_mpn_with_a_different_name_is_undecided_not_merged():
    """ADR-019: sam zgodny MPN nie scala. „Płytka ESP" (MPN ESP32-DevKitC)
    dzieli MPN z ESP32-WROOM-32 DevKit v1, ale to inna nazwa — plan pyta,
    zamiast po cichu dokleić stan."""
    client = FakeClient()
    committer = IntakeCommitter(client)
    plan = await committer.plan(
        [PartSpec.from_dict({"name": "Płytka ESP", "mpn": "ESP32-DevKitC"})], "A/A1"
    )

    assert plan.parts[0].existing_part is None
    assert plan.undecided
    assert "ESP32-WROOM-32 DevKit v1" in plan.as_text()


async def test_new_part_is_created_with_category_and_lot():
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Czujniki", mpn="HC-SR04")], "A/A1")
    refs = await committer.execute(plan)

    resources = dict(client.created)
    assert list(resources) == ["parts", "part_lots"]
    assert resources["parts"]["category"] == "/api/categories/3"
    assert resources["parts"]["manufacturer_product_number"] == "HC-SR04"
    assert resources["part_lots"]["storage_location"] == "/api/storage_locations/2"
    assert resources["part_lots"]["amount"] == 5
    assert len(refs) == 2


async def test_unknown_category_is_created_and_announced_in_the_plan():
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Wyświetlacze")], "A/A1")
    assert "NOWA kategoria: Wyświetlacze" in plan.as_text()

    await committer.execute(plan)
    assert client.created[0] == ("categories", {"name": "Wyświetlacze"})


async def test_part_without_category_is_refused_because_partdb_requires_one():
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="")], "A/A1")
    with pytest.raises(CommitError, match="kategorii"):
        await committer.execute(plan)


async def test_class_b_lot_is_marked_as_approximate():
    """ADR-007: rezystorów nie liczymy co do sztuki."""
    client = FakeClient()
    committer = IntakeCommitter(client)
    bulk = PartSpec.from_dict(
        {
            "name": "Rezystor 10k",
            "amount": 200,
            "category": "Bierne",
            "instock_unknown": True,
        }
    )

    plan = await committer.plan([bulk], "B/B1")
    assert "~200" in plan.as_text()

    await committer.execute(plan)
    assert dict(client.created)["part_lots"]["instock_unknown"] is True


async def test_uncertain_recognition_is_flagged_for_review():
    client = FakeClient()
    committer = IntakeCommitter(client)
    uncertain = PartSpec.from_dict(
        {
            "name": "Moduł matrycy LED 8x8 (MAX7219?)",
            "category": "Wyświetlacze",
            "needs_review": True,
            "note": "nadruk na kości nieczytelny, typ z układu pinów",
        }
    )

    plan = await committer.plan([uncertain], "A/A1")
    assert "[do sprawdzenia]" in plan.as_text()

    await committer.execute(plan)
    created = dict(client.created)
    assert created["parts"]["needs_review"] is True
    assert "nieczytelny" in created["parts"]["comment"]


async def test_existing_part_is_flagged_when_the_new_sighting_is_uncertain():
    client = FakeClient()
    committer = IntakeCommitter(client)
    uncertain = PartSpec.from_dict(
        {"name": "ESP32-WROOM-32 DevKit v1", "needs_review": True}
    )

    await committer.execute(await committer.plan([uncertain], "A/A1"))
    assert client.patched == [("/api/parts/1", {"needs_review": True})]


async def test_nameless_part_is_refused():
    with pytest.raises(CommitError):
        PartSpec.from_dict({"amount": 3})


# --- deduplikacja (#24) ----------------------------------------------------


def test_normalize_strips_provider_tags():
    """`BME280 [LCSC]` i `BME280` to ta sama część — nawias dokłada provider."""
    assert normalize("BME280 [LCSC]") == normalize("BME280")


def test_tokens_keep_designations_and_drop_filler():
    assert tokens("Moduł matrycy LED 8x8 MAX7219 5V") == {"max7219"}
    assert tokens("Czujnik ruchu") == set()
    assert tokens("Dioda 1N4148 THT") == {"1n4148"}


def test_decimal_point_is_part_of_the_value():
    """Zmierzone na próbniku: bez tego `1.2pF` i `12pF` to ten sam ciąg,
    a zapis dołożył stan 12 pF do pozycji 1,2 pF — dziesięciokrotna różnica
    pojemności schowana pod jedną kropką."""
    assert normalize("Kondensator 1.2pF 1206") != normalize("Kondensator 12pF 1206")
    assert normalize("Kondensator 2.2nF") != normalize("Kondensator 22nF")
    # Przecinek i kropka to ten sam separator.
    assert normalize("Kondensator 1,5pF") == normalize("Kondensator 1.5pF")


async def test_similar_values_do_not_merge_into_each_other():
    client = FakeClient(
        parts=[{"@id": "/api/parts/9", "name": "Kondensator 1.8pF 1206 C0G 50V"}]
    )
    committer = IntakeCommitter(client)
    spec_ = PartSpec.from_dict(
        {"name": "Kondensator 18pF 1206 C0G 50V", "category": "Kondensatory SMD"}
    )

    plan = await committer.plan([spec_], location="")
    assert plan.parts[0].existing_part is None, plan.as_text()
    assert not plan.undecided


def test_package_codes_are_not_designations():
    """Zmierzone na próbniku kondensatorów: `1206` jest wspólne dla całej
    obudowy, więc jako człon rozpoznawczy czyniło każdy kondensator
    „podobnym" do każdego innego i blokowało zapis osiemdziesięciu pozycji."""
    assert tokens("Kondensator 100nF 1206 X7R 16V") == {"100nf"}
    assert tokens("Kondensator 2.2pF 1206 C0G 50V") == set()


async def test_two_values_in_the_same_package_are_not_duplicates():
    client = FakeClient(
        parts=[{"@id": "/api/parts/9", "name": "Kondensator 2.5pF 1206 C0G 50V"}]
    )
    committer = IntakeCommitter(client)
    spec_ = PartSpec.from_dict(
        {"name": "Kondensator 2.7pF 1206 C0G 50V", "category": "Kondensatory SMD"}
    )

    plan = await committer.plan([spec_], location="")
    assert not plan.undecided, plan.as_text()


def test_version_in_brackets_survives_but_provider_tag_does_not():
    """`(N16R8)` to 16 MB flasha, `[LCSC]` to nazwa dostawcy. Kasowanie
    wszystkich nawiasów zlepiłoby ESP32-S3-N16R8 z ESP32-S3-N8R2."""
    assert tokens("ESP32-S3-DevKitC-1 (N16R8)") == {"esp32", "n16r8"}
    assert normalize("BME280 [LCSC]") == "bme280"
    assert normalize("ESP32-S3 (N16R8)") != normalize("ESP32-S3 (N8R2)")


async def test_similar_name_stops_the_commit_instead_of_guessing():
    """`AMS1117-3.3` i `AMS1117-5.0` to inne napięcie — scalenie po cichu
    dałoby układ zasilany złym napięciem."""
    client = FakeClient(parts=[{"@id": "/api/parts/9", "name": "AMS1117-3.3 [LCSC]"}])
    committer = IntakeCommitter(client)

    plan = await committer.plan([PartSpec.from_dict({"name": "AMS1117-5.0"})], "A/A1")

    assert plan.undecided
    assert "DO ROZSTRZYGNIĘCIA" in plan.as_text()
    assert "AMS1117-3.3 [LCSC]" in plan.as_text()

    with pytest.raises(CommitError, match="Nierozstrzygnięta"):
        await committer.execute(plan)
    assert client.created == []


async def test_merge_into_resolves_the_doubt_and_adds_a_lot():
    client = FakeClient(
        parts=[{"@id": "/api/parts/9", "name": "ESP32-S3-DevKitC-1 (N16R8)"}]
    )
    committer = IntakeCommitter(client)
    spec_ = PartSpec.from_dict(
        {"name": "Płytka ESP32-S3 16MB", "merge_into": "/api/parts/9", "amount": 2}
    )

    plan = await committer.plan([spec_], "A/A1")
    assert plan.parts[0].existing_part["@id"] == "/api/parts/9"
    assert not plan.undecided

    await committer.execute(plan)
    assert [resource for resource, _ in client.created] == ["part_lots"]


async def test_merge_into_pointing_nowhere_is_an_error():
    committer = IntakeCommitter(FakeClient())
    spec_ = PartSpec.from_dict({"name": "cokolwiek", "merge_into": "/api/parts/999"})

    with pytest.raises(CommitError, match="merge_into"):
        await committer.plan([spec_], "A/A1")


async def test_force_new_creates_a_separate_part():
    client = FakeClient(parts=[{"@id": "/api/parts/9", "name": "AMS1117-3.3"}])
    committer = IntakeCommitter(client)
    spec_ = PartSpec.from_dict(
        {"name": "AMS1117-5.0", "category": "Czujniki", "force_new": True}
    )

    plan = await committer.plan([spec_], "A/A1")
    assert not plan.undecided
    assert "utworzy część" in plan.as_text()

    await committer.execute(plan)
    assert [resource for resource, _ in client.created] == ["parts", "part_lots"]


async def test_matching_mpn_no_longer_auto_wins_over_a_different_name():
    """ADR-019: zgodny MPN nie przebija już rozbieżnej nazwy. „Cokolwiek"
    (MPN esp32 s3 n16r8) nie scala się z „Płytka ESP32-S3" po samym MPN —
    trafia do rozstrzygnięcia, a MPN zostaje sygnałem podobieństwa."""
    esp = {
        "@id": "/api/parts/9",
        "name": "Płytka ESP32-S3",
        "manufacturer_product_number": "ESP32-S3-N16R8",
    }
    client = FakeClient(
        parts=[esp, {"@id": "/api/parts/10", "name": "ESP32-S3 inna sztuka"}]
    )
    committer = IntakeCommitter(client)

    plan = await committer.plan(
        [PartSpec.from_dict({"name": "Cokolwiek", "mpn": "esp32 s3 n16r8"})], "A/A1"
    )

    assert plan.parts[0].existing_part is None
    assert plan.undecided
    assert esp in plan.parts[0].similar


# --- dyktowanie i klasa B (#29) --------------------------------------------


async def test_dictated_bucket_becomes_an_approximate_lot():
    """„dużo 100nF" ma trafić do magazynu jako stan przybliżony, a nie jako
    wymyślona dokładna liczba."""
    client = FakeClient()
    committer = IntakeCommitter(client)
    dictated = PartSpec.from_dict(
        {"name": "Kondensator 100nF 0805", "bucket": "dużo", "category": "Bierne"}
    )

    assert dictated.instock_unknown is True

    plan = await committer.plan([dictated], location="")
    assert "~200 (dużo)" in plan.as_text()

    await committer.execute(plan)
    lot = dict(client.created)["part_lots"]
    assert lot["amount"] == 200
    assert lot["instock_unknown"] is True


def test_bucket_word_in_the_amount_field_also_works():
    """Agent pisze ilość tam, gdzie ilość — nie zgaduje, które pole przyjmie
    słowo."""
    assert PartSpec.from_dict({"name": "Rezystor 10k", "amount": "sporo"}).amount == 50


def test_nothing_left_is_exact_not_approximate():
    spec_ = PartSpec.from_dict({"name": "Rezystor 10k", "bucket": "brak"})
    assert (spec_.amount, spec_.instock_unknown) == (0, False)


def test_exact_count_from_dictation_stays_exact():
    """„ze dwadzieścia elektrolitów" agent liczy sam; gdy podaje liczbę,
    nie zamieniamy jej na kubełek."""
    spec_ = PartSpec.from_dict({"name": "Kondensator 470uF", "amount": 20})
    assert (spec_.amount, spec_.instock_unknown) == (20, False)
    assert PartSpec.from_dict({"name": "x", "amount": "1,5"}).amount == 1.5


def test_made_up_quantity_word_stops_the_commit():
    with pytest.raises(CommitError, match="kubełkiem"):
        PartSpec.from_dict({"name": "Rezystor 10k", "amount": "od groma"})


# --- zdjęcie źródłowe przy części (#64) ------------------------------------


async def test_new_part_gets_the_intake_photo_as_attachment_and_thumbnail():
    """Przy module z wytartym nadrukiem własne zdjęcie jest jedynym obrazem
    tej konkretnej sztuki — katalogowe pokazuje wzorzec, nie egzemplarz."""
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Czujniki")], "A/A1", photos=[PHOTO])
    assert PHOTO.url in plan.as_text()

    refs = await committer.execute(plan)

    created = dict(client.created)
    assert created["attachments"] == {
        "name": "Zdjęcie własne (intake #17)",
        "attachment_type": "/api/attachment_types/2",
        "element": "/api/parts/101",
        "url": PHOTO.url,
    }
    # Miniatura, bo lista części bez niej pokazuje pustą ramkę.
    assert client.patched == [
        ("/api/parts/101", {"master_picture_attachment": "/api/attachments/102"})
    ]
    assert "/api/attachments/102" in refs
    assert plan.warnings == []


async def test_existing_part_does_not_collect_another_copy_of_the_same_photo():
    """Dziesiąte ujęcie tego samego modułu nie niesie już informacji."""
    client = FakeClient()
    committer = IntakeCommitter(client)
    known = PartSpec.from_dict({"name": "ESP32-WROOM-32 DevKit v1", "amount": 2})

    plan = await committer.plan([known], "A/A1", photos=[PHOTO])
    await committer.execute(plan)

    assert [resource for resource, _ in client.created] == ["part_lots"]
    assert "zdjęcie z kolejki" not in plan.as_text()


async def test_missing_photo_type_is_created_once():
    client = FakeClient(attachment_types=[])
    committer = IntakeCommitter(client)

    await committer.execute(
        await committer.plan([spec(category="Czujniki")], "", photos=[PHOTO])
    )

    assert ("attachment_types", {"name": "Zdjęcie"}) in client.created


async def test_failed_photo_does_not_undo_the_stock_write():
    """Stan magazynu jest wartością, ilustracja tylko dodatkiem — brak
    zdjęcia ma być zgłoszony, a nie wywrócić zapis kilkunastu pozycji."""

    class BrokenAttachments(FakeClient):
        async def create_attachment(self, payload):
            raise PartDbValidationError("Part-DB odrzucił załącznik (422)")

    client = BrokenAttachments()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Czujniki")], "A/A1", photos=[PHOTO])
    refs = await committer.execute(plan)

    assert [resource for resource, _ in client.created] == ["parts", "part_lots"]
    assert len(refs) == 2
    assert "nie udało się podpiąć ujęć" in plan.warnings[0]


async def test_unrelated_part_does_not_trigger_a_question():
    client = FakeClient(parts=[{"@id": "/api/parts/9", "name": "Rezystor 10k"}])
    committer = IntakeCommitter(client)

    plan = await committer.plan(
        [PartSpec.from_dict({"name": "HC-SR04", "category": "Czujniki"})], "A/A1"
    )
    assert not plan.undecided


# --- kilka ujęć jednej rzeczy (#66) ----------------------------------------


async def test_all_shots_are_attached_and_the_first_one_is_the_thumbnail():
    """Spód modułu i zbliżenie na sitodruk opisują tę samą sztukę —
    miniaturą zostaje ujęcie zrobione jako pierwsze, bo to ono pokazuje
    rzecz tak, jak leży na stole."""
    shots = [
        SourcePhoto(url="https://warsztat.example/api/intake/17/photo", item_id=17),
        SourcePhoto(url="https://warsztat.example/api/intake/18/photo", item_id=18),
    ]
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan([spec(category="Czujniki")], "A/A1", photos=shots)
    assert "2 ujęć z kolejki" in plan.as_text()

    refs = await committer.execute(plan)

    attachments = [
        payload for resource, payload in client.created if resource == "attachments"
    ]
    assert [a["url"] for a in attachments] == [shot.url for shot in shots]
    assert len({a["name"] for a in attachments}) == 2
    assert client.patched == [
        ("/api/parts/101", {"master_picture_attachment": "/api/attachments/102"})
    ]
    assert len([ref for ref in refs if ref.startswith("/api/attachments/")]) == 2
