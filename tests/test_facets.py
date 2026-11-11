"""Montaż, obudowa i wartość z nazwy części (#77) — przypadki z prawdziwego magazynu."""

import pytest

from bom_agent.facets import facets


@pytest.mark.parametrize(
    ("name", "description", "mount", "pkg"),
    [
        ("Kondensator 15pF 1206 C0G 50V", "MLCC 1206", "SMD", "1206"),
        ("Rezystor 47kΩ 1/4W 5% THT", "Rezystor węglowy przewlekany", "THT", "1/4 W"),
        ("Tranzystor S8550 PNP TO-92", "", "THT", "TO-92"),
        ("Tranzystor MOSFET N AOD4184 TO-252", "", "SMD", "TO-252"),
        ("Wzmacniacz operacyjny TL084CN DIP-14", "", "THT", "DIP-14"),
        ("STM32F205R mikrokontroler ARM Cortex-M3 LQFP-64", "", "SMD", "QFP-64"),
        ("Dioda Zenera BZX55C10 10 V 0,5 W DO-35", "", "THT", "DO-35"),
        ("Dioda LED 5mm zielona (mleczna)", "", "THT", "LED 5 mm"),
        ("Pierścień 12x LED WS2812B 5050 RGB", "", "SMD", "LED 5050"),
        # Nazwa mówi THT, opis przepisany z katalogu mówi MLCC — wygrywa nazwa.
        ("Kondensator ceramiczny 100 nF 50 V X7R THT 5 mm", "MLCC 100 nF", "THT", None),
        # Brak sygnału to brak odpowiedzi, a nie zgadywanie.
        ("Moduł ładowarki Li-Ion TP4056", "", None, None),
    ],
)
def test_mount_and_package(name, description, mount, pkg):
    found = facets(name, description)
    assert (found["m"], found["pk"]) == (mount, pkg)


def test_footprint_field_wins_over_the_name():
    assert facets("Rezystor 10k 0805", "", "1206")["pk"] == "1206"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Rezystor 5.1Ω 1/4W 1% THT", 5.1),
        ("Rezystor 910kΩ 1/4W 1% THT", 910e3),
        ("Rezystor 4k7 0805", 4700),
        ("Rezystor 4R7 0805", 4.7),
        ("Kondensator 4.3pF 1206 C0G 50V", 4.3e-12),
        ("Kondensator elektrolityczny 470µF 25V THT", 470e-6),
        ("Kondensator ceramiczny 100 nF 50 V X7R THT", 100e-9),
        # „20F” to nadruk bezpiecznika, nie 20 faradów.
        ("Bezpiecznik polimerowy PTC THT (nadruk 20F A1HH)", None),
    ],
)
def test_value_for_sorting(name, value):
    found = facets(name)["v"]
    assert found == pytest.approx(value) if value is not None else found is None
