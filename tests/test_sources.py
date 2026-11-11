"""Skąd część pochodzi i po ile — sklepy spoza katalogów (#68)."""

import pytest

from bom_agent.sources import SourceError, SourceRecorder


def part(name, *, pid=1, mpn="", description="", parameters=()):
    return {
        "@id": f"/api/parts/{pid}",
        "id": pid,
        "name": name,
        "manufacturer_product_number": mpn,
        "description": description,
        "category": {"@id": "/api/categories/3", "name": "Moduły"},
        "partLots": [],
        "parameters": list(parameters),
    }


class FakeClient:
    def __init__(self, parts, suppliers=(), orderdetails=(), attachments=()):
        self.parts = parts
        self.suppliers = list(suppliers)
        self.orderdetails = list(orderdetails)
        self.attachments = list(attachments)
        self.created: list[tuple[str, dict]] = []
        self.patched: list[tuple[str, dict]] = []
        self._next = 700

    async def list_parts(self):
        return self.parts

    async def list_suppliers(self):
        return self.suppliers

    async def list_orderdetails(self):
        return self.orderdetails

    async def list_attachments(self):
        return self.attachments

    async def list_attachment_types(self):
        return [
            {"@id": "/api/attachment_types/2", "name": "Zdjęcie"},
            {"@id": "/api/attachment_types/1", "name": "Datasheet"},
        ]

    async def create(self, resource, payload):
        self._next += 1
        self.created.append((resource, payload))
        return {"@id": f"/api/{resource}/{self._next}", **payload}

    async def create_attachment(self, payload):
        return await self.create("attachments", payload)

    async def create_parameter(self, payload):
        return await self.create("parameters", payload)

    async def patch(self, iri, payload):
        self.patched.append((iri, payload))
        return {"@id": iri, **payload}


BOTLAND_PICTURE = "https://cdn3.botland.com.pl/67781-pdt_540/rozgaleziacz-dc.jpg"


async def test_plan_shows_the_purchase_and_writes_nothing():
    client = FakeClient([part("Rozgałęziacz DC 5,5/2,1 mm")])
    plan = await SourceRecorder(client).plan(
        "Rozgałęziacz DC 5,5/2,1 mm",
        "Botland",
        supplier_part_number="KAB-08134",
        product_url="https://botland.com.pl/…/rozgaleziacz.html",
        price=9.9,
        picture_url=BOTLAND_PICTURE,
    )

    text = plan.as_text()
    assert "Botland" in text
    assert "KAB-08134" in text
    assert "9.90 zł" in text
    assert "confirm=true" in text
    assert client.created == []


async def test_confirmed_run_writes_supplier_order_price_and_photo():
    client = FakeClient([part("Rozgałęziacz DC")])
    recorder = SourceRecorder(client)

    plan = await recorder.plan(
        "Rozgałęziacz DC",
        "Botland",
        supplier_part_number="KAB-08134",
        product_url="https://botland.com.pl/x.html",
        price=9.9,
        picture_url=BOTLAND_PICTURE,
        parameters=[{"name": "Średnica zewnętrzna", "value": "5,5 mm"}],
    )
    created = await recorder.execute(plan)

    resources = [r for r, _ in client.created]
    assert resources == [
        "suppliers",
        "orderdetails",
        "pricedetails",
        "attachments",
        "parameters",
    ]

    order = dict(client.created)["orderdetails"]
    assert order["supplierpartnr"] == "KAB-08134"
    assert order["supplier_product_url"] == "https://botland.com.pl/x.html"

    price = dict(client.created)["pricedetails"]
    # Waluty nie podajemy: instancja nie ma żadnej, a puste pole znaczy
    # walutę bazową (PLN).
    assert price["price"] == "9.90"
    assert "currency" not in price

    photo = dict(client.created)["attachments"]
    assert photo["upload"]["downloadUrl"] is True
    assert len(created) == 5


async def test_shop_index_is_moved_out_of_the_manufacturer_field():
    """`KAB-08134` w polu MPN to indeks Botlandu — przez niego wyszukiwanie
    u TME i LCSC zwracało zero trafień."""
    client = FakeClient([part("Rozgałęziacz DC", mpn="KAB-08134")])
    recorder = SourceRecorder(client)

    plan = await recorder.plan(
        "Rozgałęziacz DC", "Botland", supplier_part_number="KAB-08134"
    )
    assert plan.move_mpn == "KAB-08134"
    assert "przeniosę KAB-08134" in plan.as_text()

    await recorder.execute(plan)
    assert client.patched == [("/api/parts/1", {"manufacturer_product_number": ""})]


async def test_a_real_manufacturer_number_is_left_alone():
    client = FakeClient([part("Czujnik", mpn="BME280")])
    plan = await SourceRecorder(client).plan(
        "Czujnik", "Botland", supplier_part_number="SEN-01234"
    )
    assert plan.move_mpn == ""


async def test_existing_supplier_is_reused_not_duplicated():
    client = FakeClient(
        [part("Moduł")],
        suppliers=[{"@id": "/api/suppliers/1", "name": "botland"}],
    )
    recorder = SourceRecorder(client)

    plan = await recorder.plan("Moduł", "Botland", supplier_part_number="X-1")
    await recorder.execute(plan)

    assert [r for r, _ in client.created] == ["orderdetails"]
    assert dict(client.created)["orderdetails"]["supplier"] == "/api/suppliers/1"


async def test_the_same_purchase_is_not_recorded_twice():
    client = FakeClient(
        [part("Moduł")],
        orderdetails=[
            {
                "@id": "/api/orderdetails/1",
                "part": {"@id": "/api/parts/1"},
                "supplierpartnr": "X-1",
            }
        ],
    )
    plan = await SourceRecorder(client).plan(
        "Moduł", "Botland", supplier_part_number="X-1"
    )

    assert plan.existing_order
    assert "już istnieje" in plan.as_text()


async def test_photo_the_part_already_has_is_not_added_again():
    client = FakeClient(
        [part("Moduł")],
        attachments=[
            {
                "@id": "/api/attachments/1",
                "name": "Zdjęcie ze sklepu (Botland)",
                "external_path": BOTLAND_PICTURE,
                "element": {"@id": "/api/parts/1"},
            }
        ],
    )
    plan = await SourceRecorder(client).plan(
        "Moduł", "Botland", picture_url=BOTLAND_PICTURE
    )

    assert plan.picture_url == ""
    assert "zdjęcie ze sklepu" in plan.skipped


async def test_existing_description_is_not_overwritten():
    """Opis wpisany przy rozpoznaniu ze zdjęcia mówi o TEJ sztuce; opis ze
    sklepu jest ogólny i nie ma go czym przebijać."""
    client = FakeClient([part("Moduł", description="z wytartym nadrukiem")])
    plan = await SourceRecorder(client).plan(
        "Moduł", "Botland", description="Moduł zasilacza AC/DC"
    )

    assert plan.description == ""
    assert "opis" in plan.skipped


async def test_nothing_to_write_is_refused():
    client = FakeClient([part("Moduł")])
    recorder = SourceRecorder(client)
    plan = await recorder.plan("Moduł", "Botland")

    assert plan.empty
    with pytest.raises(SourceError, match="Nie ma czego dopisać"):
        await recorder.execute(plan)
    assert client.created == []


async def test_unknown_part_and_missing_supplier_are_errors():
    client = FakeClient([part("Moduł")])
    recorder = SourceRecorder(client)

    with pytest.raises(SourceError, match="Nie ma w magazynie"):
        await recorder.plan("MAX7219", "Botland")
    with pytest.raises(SourceError, match="Podaj sklep"):
        await recorder.plan("Moduł", "")


async def test_ambiguous_part_stops_the_write():
    client = FakeClient(
        [part("Kondensator 100nF 0805", pid=1), part("Kondensator 100nF 1206", pid=2)]
    )
    with pytest.raises(SourceError, match="Nie wiadomo, o którą część"):
        await SourceRecorder(client).plan("kondensator 100nF", "Botland")
