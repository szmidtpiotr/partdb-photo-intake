"""Format zrzutu z `docs/04-model-danych.md`.

Kształty części przepisane z żywej instancji: ESP32 (wpisany ręcznie, ma lot)
i NE555 [LCSC] (zaciągnięty, `partLots: []`). Klasa B nie istnieje w danych
demo, więc tu jest wprost zbudowana — inaczej ADR-007 nie miałby ani jednego
testu.
"""

from datetime import datetime

from bom_agent.digest import CATALOG_HEADER, build_digest, short_location

NOW = datetime(2026, 8, 14, 18, 30)


def part(
    name,
    *,
    category="Mikrokontrolery i płytki",
    lots=(),
    total=None,
    needs_review=False,
    mpn="",
    description="",
):
    lots = list(lots)
    if total is None:
        total = sum(lot.get("amount") or 0 for lot in lots)
    return {
        "name": name,
        "description": description,
        "category": {"name": category, "full_path": category},
        "partLots": lots,
        "total_instock": total,
        "needs_review": needs_review,
        "manufacturer_product_number": mpn,
    }


def lot(location="Karton A → Kuweta A1", amount=6, unknown=False, refill=False):
    return {
        "storage_location": {
            "name": location.split("→")[-1].strip(),
            "full_path": location,
        },
        "amount": amount,
        "instock_unknown": unknown,
        "needs_refill": refill,
    }


def test_short_location_uses_the_code_from_each_level():
    assert short_location("Karton A → Kuweta A1") == "A/A1"
    assert short_location("Karton B → Kuweta B2 → Przegroda 07") == "B/B2/07"


def test_short_location_keeps_full_name_when_it_is_not_a_code():
    assert short_location("Szafka narzędziowa") == "Szafka narzędziowa"
    assert short_location("") == "?"


def test_exact_amount_and_location():
    text = build_digest([part("ESP32-WROOM-32 DevKit v1", lots=[lot()])], now=NOW)
    assert "=== MAGAZYN — stan na 2026-08-14 18:30 ===" in text
    assert "MIKROKONTROLERY I PŁYTKI" in text
    assert "ESP32-WROOM-32 DevKit v1" in text
    assert "x6" in text
    assert "A/A1" in text


def test_class_b_amount_is_marked_as_approximate():
    """ADR-007: rezystorów nie liczymy co do sztuki i zrzut ma to przyznać."""
    text = build_digest(
        [
            part(
                "Rezystory 10k 1/4W THT",
                category="Bierne",
                lots=[lot("Karton B → Kuweta B1", amount=200, unknown=True)],
            )
        ],
        now=NOW,
    )
    assert "~200" in text
    assert "x200" not in text
    assert "przybliżone" in text


def test_unestimated_class_b_stock_reads_as_present_not_as_empty():
    """Segregator-próbnik: wartość jest, ale nikt jej nie liczył ani nie
    szacował. `~0` czytało się dokładnie odwrotnie do prawdy — jak pozycja,
    której nie mam."""
    text = build_digest(
        [
            part(
                "Kondensator 100nF 1206 X7R 16V",
                category="Kondensatory SMD",
                lots=[lot("Karton C → Kuweta C1", amount=0, unknown=True)],
                total=0,
            )
        ],
        now=NOW,
    )
    assert "jest" in text
    assert "~0" not in text
    assert "brak" not in text


def test_needs_refill_and_needs_review_are_flagged():
    text = build_digest(
        [
            part(
                "Elektrolity 470uF 25V",
                category="Bierne",
                lots=[
                    lot("Karton B → Kuweta B2", amount=25, unknown=True, refill=True)
                ],
            ),
            part("RPi Pico", lots=[lot(amount=2)], needs_review=True),
        ],
        now=NOW,
    )
    assert "[kończy się]" in text
    assert "[do-sprawdzenia]" in text


def test_catalog_entries_go_to_a_separate_section():
    """Wpis bez PartLot nie leży w kartonie — agent nie może go użyć."""
    text = build_digest(
        [
            part("ESP32-WROOM-32 DevKit v1", lots=[lot()]),
            part("NE555 [LCSC]", category="Układy scalone", mpn="NE555P"),
        ],
        now=NOW,
    )
    stock_section, catalog_section = text.split(CATALOG_HEADER)
    assert "ESP32-WROOM-32 DevKit v1" in stock_section
    assert "NE555 [LCSC]" not in stock_section
    assert "NE555 [LCSC]" in catalog_section
    assert "NE555P" in catalog_section
    assert "1 wpis katalogowy" in text


def test_zero_stock_with_a_lot_is_not_a_catalog_entry():
    """`total_instock == 0` przy istniejącym locie znaczy „skończyło się"."""
    text = build_digest(
        [part("DHT22", category="Czujniki", lots=[lot("Karton A → Kuweta A2", 0)])],
        now=NOW,
    )
    assert CATALOG_HEADER not in text
    assert "CZUJNIKI" in text
    assert "brak" in text
    assert "skończyła się" in text


def test_multiple_lots_list_every_location():
    text = build_digest(
        [
            part(
                "Rezystory 10k",
                category="Bierne",
                lots=[
                    lot("Karton B → Kuweta B1", 100),
                    lot("Karton A → Kuweta A3", 50),
                ],
            )
        ],
        now=NOW,
    )
    assert "x150" in text
    assert "B/B1" in text
    assert "A/A3" in text


def test_parts_without_category_are_still_listed():
    text = build_digest([{"name": "Zagadka", "partLots": [lot()]}], now=NOW)
    assert "BEZ KATEGORII" in text
    assert "Zagadka" in text


def test_scope_narrows_by_category_name_and_location():
    parts = [
        part("ESP32", lots=[lot("Karton A → Kuweta A1")]),
        part("HC-SR04", category="Czujniki", lots=[lot("Karton A → Kuweta A2")]),
    ]
    by_category = build_digest(parts, now=NOW, scope="czujniki")
    assert "HC-SR04" in by_category
    assert "ESP32" not in by_category
    assert "(zakres: czujniki)" in by_category

    by_location = build_digest(parts, now=NOW, scope="A/A1")
    assert "ESP32" in by_location
    assert "HC-SR04" not in by_location


def test_empty_inventory_says_so_instead_of_printing_nothing():
    text = build_digest([], now=NOW)
    assert "Magazyn pusty" in text


def test_stock_without_a_container_gets_its_own_section():
    """ADR-017: „mam, ale nie wiem gdzie" to co innego niż wpis katalogowy
    i co innego niż część leżąca w kuwecie."""
    lot_bez_miejsca = {"amount": 1, "instock_unknown": False, "needs_refill": False}
    text = build_digest(
        [
            part("ESP32-S3-DevKitC-1", lots=[lot_bez_miejsca], total=1),
            part("HC-SR04", category="Czujniki", lots=[lot("Karton A → Kuweta A2", 5)]),
            part("NE555 [LCSC]", category="Układy scalone", mpn="NE555"),
        ],
        now=NOW,
    )

    assert "MAM, ALE BEZ PRZYPISANIA" in text
    stock_part, rest = text.split("MAM, ALE BEZ PRZYPISANIA")
    assert "HC-SR04" in stock_part
    assert "ESP32-S3-DevKitC-1" in rest.split(CATALOG_HEADER)[0]
    assert "1 pozycja bez przypisanego pojemnika" in text
    assert "lista robocza" in text


def test_unassigned_stock_still_counts_as_owned_for_catalog_twins():
    lot_bez_miejsca = {"amount": 3}
    text = build_digest(
        [
            part("BME280", category="Czujniki", lots=[lot_bez_miejsca], total=3),
            part("BME280 [LCSC]", category="Czujniki", mpn="BME280"),
        ],
        now=NOW,
    )
    assert "to samo co: BME280 (na stanie)" in text


def test_catalog_entry_points_at_its_twin_on_the_shelf():
    """„BME280 [LCSC]" i „BME280" to ta sama część — bez adnotacji agent
    uznałby, że czujnika nie ma, choć leży w kuwecie A2."""
    text = build_digest(
        [
            part("BME280", category="Czujniki", lots=[lot("Karton A → Kuweta A2", 3)]),
            part("BME280 [LCSC]", category="Czujniki", mpn="BME280"),
        ],
        now=NOW,
    )
    assert "to samo co: BME280 (na stanie)" in text


def test_catalog_entry_without_a_twin_gets_no_annotation():
    text = build_digest(
        [
            part("ESP32", lots=[lot()]),
            part("NE555 [LCSC]", category="Układy scalone", mpn="NE555"),
        ],
        now=NOW,
    )
    assert "to samo co" not in text


def test_amount_column_stays_aligned_with_long_names():
    parts = [
        part("BC547", category="Bierne", lots=[lot("Karton B → Kuweta B2", 30)]),
        part(
            "Kondensator elektrolityczny 470 µF 25 V",
            category="Bierne",
            lots=[lot("Karton B → Kuweta B2", 25)],
        ),
    ]
    text = build_digest(parts, now=NOW)
    columns = {line.index("x") for line in text.splitlines() if " x" in line}
    assert len(columns) == 1, f"ilości nie stoją w jednej kolumnie: {text}"


def test_sections_and_parts_are_sorted_predictably():
    parts = [
        part("Zenerka", category="Półprzewodniki", lots=[lot()]),
        part("Arduino Nano", lots=[lot()]),
        part("ESP32", lots=[lot()]),
    ]
    text = build_digest(parts, now=NOW)
    assert text.index("MIKROKONTROLERY") < text.index("PÓŁPRZEWODNIKI")
    assert text.index("Arduino Nano") < text.index("ESP32")
