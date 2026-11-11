"""Przypisywanie pojemnika rzeczom leżącym luzem (#62, ADR-017)."""

import pytest

from bom_agent.assign import AssignError, LocationAssigner

LOCATIONS = [
    {
        "@id": "/api/storage_locations/1",
        "name": "Kuweta A1",
        "full_path": "Karton A → Kuweta A1",
    },
    {
        "@id": "/api/storage_locations/2",
        "name": "Kuweta B2",
        "full_path": "Karton B → Kuweta B2",
    },
]


def part(name, *, lots=(), category="Czujniki", pid=1, mpn=""):
    return {
        "@id": f"/api/parts/{pid}",
        "id": pid,
        "name": name,
        "manufacturer_product_number": mpn,
        "category": {
            "@id": "/api/categories/3",
            "name": category,
            "full_path": category,
        },
        "partLots": list(lots),
    }


def lot(lot_id, *, amount=3, where=None, unknown=False, description=""):
    data = {
        "@id": f"/api/part_lots/{lot_id}",
        "id": lot_id,
        "amount": amount,
        "instock_unknown": unknown,
        "description": description,
    }
    if where:
        data["storage_location"] = where
    return data


class FakeClient:
    def __init__(self, parts, locations=None):
        self.parts = parts
        self.locations = LOCATIONS if locations is None else locations
        self.patched: list[tuple[str, dict]] = []

    async def list_parts(self):
        return self.parts

    async def list_storage_locations(self):
        return self.locations

    async def patch(self, iri, payload):
        self.patched.append((iri, payload))
        return {"@id": iri, **payload}


async def test_plan_shows_what_will_move_and_writes_nothing():
    client = FakeClient([part("HC-SR04", lots=[lot(10, amount=4)], pid=7)])
    assigner = LocationAssigner(client)

    plan = await assigner.plan("A/A1", parts=["HC-SR04"])
    text = plan.as_text()

    assert "Karton A → Kuweta A1" in text
    assert "HC-SR04" in text and "x4" in text
    assert "confirm=true" in text
    assert client.patched == []


async def test_confirmed_assignment_sets_the_location_on_the_lot():
    client = FakeClient([part("HC-SR04", lots=[lot(10)], pid=7)])
    assigner = LocationAssigner(client)

    plan = await assigner.plan("Kuweta A1", parts=["hc sr04"])
    changed = await assigner.execute(plan)

    assert client.patched == [
        ("/api/part_lots/10", {"storage_location": "/api/storage_locations/1"})
    ]
    assert changed == ["/api/part_lots/10"]


async def test_a_lot_that_already_has_a_box_is_skipped_not_moved():
    """Przeprowadzka rzeczy już położonej to inna operacja (#38). Cicha
    zmiana oznaczałaby, że część „przeniosła się" sama, bez wiedzy człowieka."""
    placed = lot(
        11,
        where={"@id": "/api/storage_locations/2", "full_path": "Karton B → Kuweta B2"},
    )
    client = FakeClient([part("BME280", lots=[placed], pid=8)])
    assigner = LocationAssigner(client)

    plan = await assigner.plan("A/A1", parts=["BME280"])

    assert plan.lots == []
    assert plan.already_placed == [("BME280", "Karton B → Kuweta B2")]
    assert "POMIJAM, bo już mają pojemnik" in plan.as_text()
    with pytest.raises(AssignError, match="Nie ma czego przypisać"):
        await assigner.execute(plan)
    assert client.patched == []


async def test_catalog_entry_without_stock_cannot_be_put_in_a_box():
    client = FakeClient([part("BME280 [LCSC]", lots=[], pid=9)])
    assigner = LocationAssigner(client)

    plan = await assigner.plan("A/A1", parts=["BME280 [LCSC]"])

    assert plan.without_stock == ["BME280 [LCSC]"]
    assert "wpisy katalogowe bez stanu" in plan.as_text()


async def test_ambiguous_name_stops_the_write():
    """Wsadzenie nie tej części do kuwety jest gorsze niż niewsadzenie żadnej."""
    client = FakeClient(
        [
            part("Kondensator 100nF 0805", lots=[lot(1)], pid=1),
            part("Kondensator 100nF 1206", lots=[lot(2)], pid=2),
        ]
    )
    assigner = LocationAssigner(client)

    plan = await assigner.plan("A/A1", parts=["kondensator 100nF"])

    assert plan.blocked
    assert plan.lots == []
    assert "WSTRZYMANE" in plan.as_text()
    with pytest.raises(AssignError, match="Nierozstrzygnięte"):
        await assigner.execute(plan)


async def test_exact_name_wins_over_similar_ones():
    client = FakeClient(
        [
            part("ESP32-WROOM-32 DevKit v1", lots=[lot(3)], pid=1),
            part("ESP32-S3 DevKit", lots=[lot(4)], pid=2),
        ]
    )
    assigner = LocationAssigner(client)

    plan = await assigner.plan("A/A1", parts=["esp32 wroom 32 devkit v1"])

    assert [lot_.part_name for lot_ in plan.lots] == ["ESP32-WROOM-32 DevKit v1"]


async def test_whole_category_goes_into_one_box():
    """Przy sortowaniu to najczęstszy ruch: cała szuflada kondensatorów naraz."""
    client = FakeClient(
        [
            part(
                "Kondensator 100nF", lots=[lot(1)], category="Kondensatory SMD", pid=1
            ),
            part("Kondensator 1uF", lots=[lot(2)], category="Kondensatory SMD", pid=2),
            part("HC-SR04", lots=[lot(3)], category="Czujniki", pid=3),
        ]
    )
    assigner = LocationAssigner(client)

    plan = await assigner.plan("A/A1", category="Kondensatory SMD")
    await assigner.execute(plan)

    assert [iri for iri, _ in client.patched] == [
        "/api/part_lots/1",
        "/api/part_lots/2",
    ]


async def test_unknown_box_is_an_error_with_the_list_of_real_ones():
    """Pojemnika nie wymyślamy — rzecz „leżąca" w nieistniejącym pudle
    jest gorsza niż rzecz bez przypisania (ADR-017)."""
    client = FakeClient([part("HC-SR04", lots=[lot(1)])])
    assigner = LocationAssigner(client)

    with pytest.raises(AssignError, match="Nie ma lokalizacji"):
        await assigner.plan("Kuweta Z9", parts=["HC-SR04"])
    assert client.patched == []


async def test_empty_location_is_refused_because_that_is_what_intake_does():
    client = FakeClient([part("HC-SR04", lots=[lot(1)])])
    with pytest.raises(AssignError, match="Pusta lokalizacja"):
        await LocationAssigner(client).plan("", parts=["HC-SR04"])


async def test_selection_has_to_be_explicit():
    client = FakeClient([part("HC-SR04", lots=[lot(1)])])
    assigner = LocationAssigner(client)

    with pytest.raises(AssignError, match="Podaj, co przypisać"):
        await assigner.plan("A/A1")
    with pytest.raises(AssignError, match="albo listę części, albo kategorię"):
        await assigner.plan("A/A1", parts=["HC-SR04"], category="Czujniki")


async def test_unknown_part_is_reported_not_guessed():
    client = FakeClient([part("HC-SR04", lots=[lot(1)])])
    plan = await LocationAssigner(client).plan("A/A1", parts=["MAX7219"])

    assert plan.unknown == ["MAX7219"]
    assert plan.lots == []


async def test_bucket_amounts_are_shown_as_approximate():
    """Klasa B (ADR-007) ma się czytać jak przybliżenie, nie jak pomiar."""
    client = FakeClient(
        [part("Kondensator 100nF", lots=[lot(1, amount=200, unknown=True)], pid=1)]
    )
    plan = await LocationAssigner(client).plan("A/A1", parts=["Kondensator 100nF"])

    assert plan.lots[0].amount == "~200"
