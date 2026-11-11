"""Zapisy z karty części Warsztatu (#77, ADR-022 etap 2) — na udawanym Part-DB."""

import pytest

from bom_agent import warsztat_edit as edit


class FakePartDb:
    def __init__(self):
        self.parts = {
            1: {
                "id": 1,
                "@id": "/api/parts/1",
                "name": "BC547",
                "tags": "",
                "parameters": [],
                "attachments": [],
                "partLots": [
                    {
                        "@id": "/api/part_lots/10",
                        "id": 10,
                        "amount": 4,
                        "instock_unknown": False,
                    }
                ],
            }
        }
        self.locations = [
            {
                "@id": "/api/storage_locations/50",
                "id": 50,
                "name": "Karton A",
                "full_path": "Karton A",
            },
            {
                "@id": "/api/storage_locations/51",
                "id": 51,
                "name": "Kuweta A1",
                "full_path": "Karton A → Kuweta A1",
            },
        ]
        self.calls = []

    async def get_part(self, part_id):
        return self.parts[int(part_id)]

    async def patch(self, iri, payload):
        self.calls.append(("patch", iri, payload))
        if iri.startswith("/api/parts/"):
            self.parts[int(iri.rsplit("/", 1)[1])].update(payload)
        return payload

    async def update_part_lot(self, lot_id, payload):
        self.calls.append(("lot", lot_id, payload))
        for part in self.parts.values():
            for lot in part["partLots"]:
                if lot["id"] == lot_id:
                    lot.update(payload)
        return payload

    async def create_part_lot(self, payload):
        self.calls.append(("new_lot", payload))
        lot = {"@id": "/api/part_lots/99", "id": 99, **payload}
        self.parts[1]["partLots"].append(lot)
        return lot

    async def delete(self, iri):
        self.calls.append(("delete", iri))

    async def create(self, resource, payload):
        self.calls.append(("create", resource, payload))
        return {"@id": f"/api/{resource}/7", "id": 7, **payload}

    async def list_all(self, resource):
        return [{"@id": "/api/manufacturers/3", "name": "Texas Instruments"}]

    async def list_storage_locations(self):
        return self.locations


@pytest.fixture
def db():
    return FakePartDb()


async def test_fields_become_partdb_columns_and_mount_becomes_a_tag(db):
    await edit.update_fields(
        db,
        1,
        {
            "name": " BC547B ",
            "category": 14,
            "checked": True,
            "mount": "THT",
            "manufacturer": "texas instruments",
        },
    )
    part = db.parts[1]
    assert part["name"] == "BC547B"
    assert part["category"] == "/api/categories/14"
    assert part["needs_review"] is False
    assert part["tags"] == "THT"
    assert part["manufacturer"] == "/api/manufacturers/3"  # istniejący, bez duplikatu


async def test_empty_name_is_refused(db):
    with pytest.raises(edit.EditError):
        await edit.update_fields(db, 1, {"name": "  "})


async def test_quantity_shapes_map_to_the_single_lot(db):
    await edit.set_quantity(db, 1, "approx", 200, True)
    assert db.calls[-1] == (
        "lot",
        10,
        {"amount": 200.0, "instock_unknown": True, "needs_refill": True},
    )
    await edit.set_quantity(db, 1, "some", None, False)
    assert db.calls[-1][2]["amount"] == 0 and db.calls[-1][2]["instock_unknown"] is True


async def test_quantity_in_two_boxes_is_not_guessed(db):
    db.parts[1]["partLots"].append(
        {
            "id": 11,
            "amount": 2,
            "storage_location": {"@id": "/api/storage_locations/51"},
        }
    )
    with pytest.raises(edit.EditError, match="kilku pudełkach"):
        await edit.set_quantity(db, 1, "exact", 10, False)


async def test_take_one_only_for_counted_stock(db):
    await edit.take_one(db, 1)
    assert db.parts[1]["partLots"][0]["amount"] == 3
    db.parts[1]["partLots"][0]["instock_unknown"] = True
    with pytest.raises(edit.EditError):
        await edit.take_one(db, 1)


async def test_assign_puts_loose_stock_in_the_box_then_second_box_gets_a_new_lot(db):
    await edit.assign_box(db, 1, 51)
    assert db.calls[-1] == (
        "lot",
        10,
        {"storage_location": "/api/storage_locations/51"},
    )
    await edit.assign_box(db, 1, 50)
    assert db.calls[-1][0] == "new_lot"
    assert db.calls[-1][1]["storage_location"] == "/api/storage_locations/50"
    with pytest.raises(edit.EditError, match="już jest"):
        await edit.assign_box(db, 1, 50)


async def test_removing_the_only_lot_from_a_box_keeps_the_stock_loose(db):
    db.parts[1]["partLots"][0]["storage_location"] = {
        "@id": "/api/storage_locations/51"
    }
    await edit.remove_from_box(db, 1, 51)
    assert db.calls[-1] == ("lot", 10, {"storage_location": None})


def test_next_box_names_follow_the_carton_tray_compartment_scheme(db):
    assert edit.next_box_name(db.locations, None) == ("Karton B", "B")
    assert edit.next_box_name(db.locations, 50) == ("Kuweta A2", "A/A2")
    assert edit.next_box_name(db.locations, 51) == ("Przegroda 01", "A/A1/01")


async def test_delete_needs_the_exact_name(db):
    with pytest.raises(edit.EditError):
        await edit.delete_part(db, 1, "BC548")
    await edit.delete_part(db, 1, "bc547")
    assert db.calls[-1] == ("delete", "/api/parts/1")


def test_shop_label_from_address():
    assert edit._shop_label("https://botland.com.pl/moduly/123") == "Botland"
    assert edit._shop_label("https://www.tme.eu/pl/details/x") == "Tme"
