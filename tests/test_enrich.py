"""Zdjęcia katalogowe, datasheety i parametry od dostawcy (#67)."""

import pytest

from bom_agent.enrich import EnrichError, PartEnricher
from bom_agent.partdb_client import PartDbUnavailable


def part(name, *, pid=1, mpn="", category="Czujniki", parameters=()):
    return {
        "@id": f"/api/parts/{pid}",
        "id": pid,
        "name": name,
        "manufacturer_product_number": mpn,
        "category": {"@id": "/api/categories/3", "name": category},
        "partLots": [],
        "parameters": list(parameters),
    }


class FakeClient:
    """Podstawka pod Part-DB z dwoma dostawcami."""

    def __init__(self, parts, results=None, details=None, attachments=()):
        self.parts = parts
        self.results = results if results is not None else []
        self.details = details or {}
        self.attachments = list(attachments)
        self.created: list[tuple[str, dict]] = []
        self.searches: list[tuple[str, list[str]]] = []
        self._next = 500

    async def list_parts(self):
        return self.parts

    async def list_attachments(self):
        return self.attachments

    async def list_attachment_types(self):
        return [
            {"@id": "/api/attachment_types/2", "name": "Zdjęcie"},
            {"@id": "/api/attachment_types/1", "name": "Datasheet"},
        ]

    async def list_info_providers(self):
        return [{"key": "lcsc"}, {"key": "tme"}]

    async def search_info_providers(self, keyword, providers=None):
        self.searches.append((keyword, providers or []))
        return self.results

    async def info_provider_details(self, provider_key, provider_id):
        return self.details.get(provider_id, {})

    async def create(self, resource, payload):
        self._next += 1
        self.created.append((resource, payload))
        return {"@id": f"/api/{resource}/{self._next}", **payload}

    async def create_attachment(self, payload):
        return await self.create("attachments", payload)

    async def create_parameter(self, payload):
        return await self.create("parameters", payload)


LCSC_HIT = {
    "provider_key": "lcsc",
    "provider_id": "C5125085",
    "name": "NE555",
    "mpn": "NE555",
    "provider_url": "https://www.lcsc.com/product-detail/C5125085.html",
}

LCSC_DETAILS = {
    "C5125085": {
        "images": [{"url": "https://assets.lcsc.com/…/NE555_front.jpg"}],
        "datasheets": [{"url": "https://datasheet.lcsc.com/…/NE555.pdf"}],
        "parameters": [
            {"name": "Supply Voltage", "value_typ": 5, "unit": "V"},
            {"name": "Package", "value_text": "DIP-8"},
        ],
    }
}


async def test_plan_lists_what_will_be_added_and_writes_nothing():
    client = FakeClient(
        [part("NE555", mpn="NE555")], results=[LCSC_HIT], details=LCSC_DETAILS
    )
    plan = await PartEnricher(client).plan(parts=["NE555"])

    text = plan.as_text()
    assert "zdjęcie katalogowe" in text
    assert "datasheet" in text
    assert "2 parametrów" in text
    assert "zgodność po: MPN" in text
    assert client.created == []


async def test_confirmed_run_downloads_the_file_into_partdb():
    """Link do sklepu przeżyje kilka miesięcy, część leży w warsztacie latami."""
    client = FakeClient(
        [part("NE555", mpn="NE555")], results=[LCSC_HIT], details=LCSC_DETAILS
    )
    enricher = PartEnricher(client)

    plan = await enricher.plan(parts=["NE555"])
    created = await enricher.execute(plan)

    attachments = [p for r, p in client.created if r == "attachments"]
    assert [a["url"] for a in attachments] == [
        "https://assets.lcsc.com/…/NE555_front.jpg",
        "https://datasheet.lcsc.com/…/NE555.pdf",
    ]
    assert all(a["upload"]["downloadUrl"] is True for a in attachments)
    # Miniaturą zostaje własne zdjęcie, jeśli część je ma — katalogowe
    # wchodzi tylko na puste miejsce.
    assert attachments[0]["upload"]["becomePreviewIfEmpty"] is True
    assert attachments[1]["upload"]["becomePreviewIfEmpty"] is False

    parameters = [p for r, p in client.created if r == "parameters"]
    assert parameters[0]["name"] == "Supply Voltage"
    # `value_typ` u dostawcy to `value_typical` w Part-DB — cicha utrata
    # tej wartości zostawiłaby parametr bez liczby.
    assert parameters[0]["value_typical"] == 5
    assert parameters[0]["element"] == "/api/parts/1"
    assert len(created) == 4


async def test_a_name_only_guess_is_refused():
    """Zapytanie TME o NE555 zwróciło płytkę MIKROE-2032 — automat dokleiłby
    do układu scalonego zdjęcie czegoś zupełnie innego."""
    wrong = {
        "provider_key": "tme",
        "provider_id": "MIKROE-2032",
        "name": "Klik płytka NE555",
        "mpn": "MIKROE-2032",
    }
    client = FakeClient([part("NE555")], results=[wrong], details={})

    plan = await PartEnricher(client).plan(parts=["NE555"])

    assert plan.items == []
    assert plan.no_provider_match[0][0] == "NE555"
    assert "MIKROE" in plan.as_text()
    assert client.created == []


async def test_exact_name_match_counts_when_there_is_no_mpn():
    hit = {**LCSC_HIT, "mpn": ""}
    client = FakeClient([part("NE555")], results=[hit], details=LCSC_DETAILS)

    plan = await PartEnricher(client).plan(parts=["NE555"])

    assert plan.items[0].hit.matched_by == "nazwa"


async def test_what_the_part_already_has_is_not_added_twice():
    existing = [
        {
            "@id": "/api/attachments/8",
            "name": "Zdjęcie własne (intake #17)",
            "external_path": "https://assets.lcsc.com/…/NE555_front.jpg",
            "picture": True,
            "element": {"@id": "/api/parts/1"},
        }
    ]
    client = FakeClient(
        [part("NE555", mpn="NE555")],
        results=[LCSC_HIT],
        details=LCSC_DETAILS,
        attachments=existing,
    )

    plan = await PartEnricher(client).plan(parts=["NE555"])

    assert plan.items[0].picture_url == ""
    assert "zdjęcie katalogowe" in plan.items[0].skipped


async def test_existing_parameters_are_left_alone():
    client = FakeClient(
        [part("NE555", mpn="NE555", parameters=[{"name": "Package"}])],
        results=[LCSC_HIT],
        details=LCSC_DETAILS,
    )

    plan = await PartEnricher(client).plan(parts=["NE555"])

    assert [p["name"] for p in plan.items[0].parameters] == ["Supply Voltage"]


async def test_search_uses_the_mpn_when_the_part_has_one():
    """Nazwa własna („Kondensator 100nF 1206") nic dostawcy nie mówi."""
    client = FakeClient(
        [part("Timer NE555 w DIP", mpn="NE555")],
        results=[LCSC_HIT],
        details=LCSC_DETAILS,
    )
    await PartEnricher(client).plan(parts=["parts/1"])

    assert client.searches == [("NE555", ["lcsc", "tme"])]


async def test_limit_is_reported_not_silent():
    """Cicha obcinka czytałaby się jak „zrobione wszystko"."""
    parts = [part(f"Kondensator {i}", pid=i, category="Kondensatory") for i in range(5)]
    client = FakeClient(parts, results=[], details={})

    plan = await PartEnricher(client).plan(category="Kondensatory", limit=2)

    assert plan.truncated == 3
    assert "UCIĘTE" in plan.as_text()


async def test_nothing_to_add_is_not_a_write():
    client = FakeClient(
        [part("NE555", mpn="NE555")],
        results=[LCSC_HIT],
        details={"C5125085": {"images": [], "datasheets": [], "parameters": []}},
    )
    enricher = PartEnricher(client)
    plan = await enricher.plan(parts=["NE555"])

    assert plan.useful == []
    with pytest.raises(EnrichError, match="Nie ma czego dołożyć"):
        await enricher.execute(plan)
    assert client.created == []


async def test_ambiguous_part_stops_before_asking_the_provider():
    client = FakeClient(
        [part("Kondensator 100nF 0805", pid=1), part("Kondensator 100nF 1206", pid=2)],
        results=[LCSC_HIT],
    )
    plan = await PartEnricher(client).plan(parts=["kondensator 100nF"])

    assert plan.blocked
    assert client.searches == []


async def test_selection_has_to_be_explicit():
    client = FakeClient([part("NE555")])
    with pytest.raises(EnrichError, match="Podaj, co uzupełnić"):
        await PartEnricher(client).plan()


async def test_designation_is_pulled_out_of_a_human_name():
    """Część spisana ze zdjęcia nazywa się „HC-SR501 czujnik ruchu PIR" i nie
    ma numeru katalogowego. Zapytanie pełną nazwą nie trafia w nic."""
    hit = {
        "provider_key": "lcsc",
        "provider_id": "C123",
        "name": "PIR Motion Sensor Module",
        "mpn": "HC-SR501",
    }
    client = FakeClient(
        [part("HC-SR501 czujnik ruchu PIR")],
        results=[hit],
        details={"C123": {"images": [{"url": "https://x/pir.jpg"}]}},
    )

    plan = await PartEnricher(client).plan(parts=["parts/1"])

    assert client.searches == [("HC-SR501", ["lcsc", "tme"])]
    assert plan.items[0].hit.matched_by == "oznaczenie HC-SR501"


async def test_user_can_point_at_the_right_provider_record():
    """Gdy nazwa handlowa nie zgadza się z niczym, człowiek wskazuje rekord —
    i to jest jedyna droga, którą wolno ominąć dopasowywanie."""
    client = FakeClient(
        [part("Zasilacz AC/DC 5V z Aliexpress")],
        results=[],
        details={"C5125085": {"datasheets": [{"url": "https://x/ac-dc.pdf"}]}},
    )
    enricher = PartEnricher(client)

    plan = await enricher.plan(
        parts=["parts/1"], provider_key="lcsc", provider_id="C5125085"
    )

    assert plan.items[0].hit.matched_by == "wskazane ręcznie"
    assert plan.items[0].datasheet_url == "https://x/ac-dc.pdf"
    assert client.searches == []


async def test_pointing_at_a_record_needs_exactly_one_part():
    client = FakeClient(
        [part("A", pid=1, category="X"), part("B", pid=2, category="X")]
    )
    with pytest.raises(EnrichError, match="tylko dla JEDNEJ"):
        await PartEnricher(client).plan(
            category="X", provider_key="lcsc", provider_id="C1"
        )


async def test_half_a_pick_is_refused():
    client = FakeClient([part("NE555")])
    with pytest.raises(EnrichError, match="Podaj oba"):
        await PartEnricher(client).plan(parts=["NE555"], provider_key="lcsc")


async def test_thumbnail_from_the_search_result_is_used_when_details_have_none():
    """TME nie oddaje zdjęć w szczegółach — ma tylko miniaturę w wynikach
    wyszukiwania. Bez niej część z TME zostawałaby bez żadnego obrazu."""
    tme_hit = {
        "provider_key": "tme",
        "provider_id": "KAB-08134",
        "name": "Rozgałęziacz DC",
        "mpn": "KAB-08134",
        "preview_image_url": "https://cdn.tme.eu/kab-08134.jpg",
    }
    client = FakeClient(
        [part("Rozgałęziacz DC", mpn="KAB-08134")],
        results=[tme_hit],
        details={"KAB-08134": {"images": [], "datasheets": [], "parameters": []}},
    )

    plan = await PartEnricher(client).plan(parts=["KAB-08134"])

    assert plan.items[0].picture_url == "https://cdn.tme.eu/kab-08134.jpg"


async def test_search_phrase_drops_the_parenthetical_and_the_plus():
    """Nazwa z dopiskiem w nawiasie i znakiem `+` wywracała zapytanie do TME
    (400 → 500). Do dostawcy ma iść sam człon handlowy (#71)."""
    client = FakeClient(
        [part("WeMos D1 mini Battery Shield (ładowarka Li-Po + step-up 5V)")],
        results=[],
    )
    await PartEnricher(client).plan(parts=["parts/1"])

    assert client.searches == [("WeMos D1 mini Battery Shield", ["lcsc", "tme"])]


async def test_a_name_that_is_only_a_parenthetical_asks_nothing():
    """Nie ma czym pytać, gdy po obcięciu nawiasu nic nie zostaje —
    zamiast pustego zapytania po prostu „nie znalazłem"."""
    client = FakeClient([part("(zestaw z Aliexpress)")], results=[LCSC_HIT])

    plan = await PartEnricher(client).plan(parts=["parts/1"])

    assert client.searches == []
    assert plan.no_provider_match[0][0] == "(zestaw z Aliexpress)"


async def test_one_providers_rejection_does_not_sink_the_batch():
    """Jedna zła nazwa (400 z TME → 500) nie może zablokować wzbogacania
    pozostałych pozycji partii intake'u (#71)."""

    class Picky(FakeClient):
        async def search_info_providers(self, keyword, providers=None):
            self.searches.append((keyword, providers or []))
            if "Battery Shield" in keyword:
                raise PartDbUnavailable(
                    "Part-DB zwrócił błąd serwera (500): Internal Server Error"
                )
            return self.results

    parts = [
        part("WeMos D1 mini Battery Shield", pid=1),
        part("NE555", pid=2, mpn="NE555"),
    ]
    client = Picky(parts, results=[LCSC_HIT], details=LCSC_DETAILS)

    plan = await PartEnricher(client).plan(parts=["parts/1", "parts/2"])

    # NE555 przeszło mimo awarii przy pierwszej części.
    assert [item.part_name for item in plan.items] == ["NE555"]
    assert plan.no_provider_match[0][0] == "WeMos D1 mini Battery Shield"
    assert any("odrzucił zapytanie" in w for w in plan.warnings)
    assert client.created == []


async def test_instance_without_downloads_still_gets_the_link():
    """Pobieranie plików jest w Part-DB domyślnie wyłączone. Sam odnośnik
    jest wtedy lepszy niż nic — ale to musi być powiedziane wprost."""

    class NoDownloads(FakeClient):
        async def create_attachment(self, payload):
            if "upload" in payload:
                raise PartDbUnavailable(
                    "Part-DB zwrócił błąd serwera (500): "
                    "Download of attachments is not allowed!"
                )
            return await self.create("attachments", payload)

    client = NoDownloads(
        [part("NE555", mpn="NE555")], results=[LCSC_HIT], details=LCSC_DETAILS
    )
    enricher = PartEnricher(client)

    plan = await enricher.plan(parts=["NE555"])
    await enricher.execute(plan)

    attachments = [p for r, p in client.created if r == "attachments"]
    assert len(attachments) == 2
    assert all("upload" not in a for a in attachments)
    assert "ALLOW_ATTACHMENT_DOWNLOADS" in plan.warnings[0]
