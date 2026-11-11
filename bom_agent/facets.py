"""Montaż, obudowa i wartość części — odczytane z nazwy (#77, ADR-022).

Przy szukaniu rezystora pierwsze pytania to „THT czy SMD?”, a przy SMD
„jaki rozmiar?”. Part-DB ma na to pole `footprint`, ale w tym magazynie jest
prawie zawsze puste: rozpoznanie ze zdjęć wpisuje obudowę do nazwy
(„Kondensator 15pF 1206 C0G 50V”, „Tranzystor S8550 PNP TO-92”), bo tak ją
widać na zdjęciu. Wyciągamy ją więc stamtąd.

To jest **widok**, nie zapis: nic nie trafia do Part-DB, a wynik liczy się
od nowa przy każdym zrzucie. Gdy kiedyś obudowy trafią do pola `footprint`,
ono wygrywa z odczytem z nazwy.

Reguła ostrożności: gdy nie ma pewnego sygnału, nie zgadujemy. Część bez
rozpoznanej obudowy trafia do grupy „inne”, a nie do najbardziej
prawdopodobnej — fałszywy filtr ukryłby ją dokładnie wtedy, gdy jej szukasz.
"""

from __future__ import annotations

import re

# Rozmiary chipów SMD (calowe kody EIA). Cztery cyfry w nazwie bywają też
# rokiem albo numerem katalogowym, dlatego tylko ta zamknięta lista.
CHIP_SIZES = (
    "01005",
    "0201",
    "0402",
    "0603",
    "0805",
    "1206",
    "1210",
    "1812",
    "2010",
    "2512",
)

# (wzorzec, nazwa wyświetlana, montaż). Kolejność ma znaczenie: dłuższe
# i bardziej szczegółowe przed ogólnymi (SOT-223 przed SOT-23).
PACKAGES: list[tuple[str, str, str]] = [
    (r"\b(" + "|".join(CHIP_SIZES) + r")\b", r"\1", "SMD"),
    (r"\bSOT-?223\b", "SOT-223", "SMD"),
    (r"\bSOT-?23(?:-(\d))?\b", "SOT-23", "SMD"),
    (r"\bSOT-?89\b", "SOT-89", "SMD"),
    (r"\bSOD-?(123|323|523)\b", r"SOD-\1", "SMD"),
    (r"\bT?SSOP-?(\d+)\b", r"SSOP-\1", "SMD"),
    (r"\bMSOP-?(\d+)\b", r"MSOP-\1", "SMD"),
    (r"\bSO(?:IC|P)-?(\d+)\b", r"SOIC-\1", "SMD"),
    (r"\b[LT]QFP-?(\d+)\b", r"QFP-\1", "SMD"),
    (r"\bQFN-?(\d+)?\b", "QFN", "SMD"),
    (r"\b(?:TO-?252|DPAK)\b", "TO-252", "SMD"),
    (r"\b(?:TO-?263|D2PAK)\b", "TO-263", "SMD"),
    (r"\b(5050|3528|2835)\b(?=.*\b(?:LED|WS28|RGB))", r"LED \1", "SMD"),
    (r"\bDIP-?(\d+)\b", r"DIP-\1", "THT"),
    (r"\bTO-?92\b", "TO-92", "THT"),
    (r"\bTO-?220\b", "TO-220", "THT"),
    (r"\bTO-?247\b", "TO-247", "THT"),
    (r"\bDO-?(35|41|15|201)\b", r"DO-\1", "THT"),
    (r"\bHC-?49\b", "HC-49", "THT"),
    (r"\b([35]) ?mm\b(?=.*\bLED\b)|\bLED\b.*\b([35]) ?mm\b", "LED {mm} mm", "THT"),
]

THT_WORDS = re.compile(r"\bTHT\b|przewlekan|promieniow|radial|osiow|axial", re.I)
SMD_WORDS = re.compile(r"\bSMD\b|\bMLCC\b", re.I)

# Mnożniki jednostek. „µ” i „u” to to samo, a „R” w zapisie „4R7” jest
# przecinkiem dziesiętnym, nie omem.
PREFIX = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
}
# Pojemność tylko z przedrostkiem: gołe „20F” to w tym magazynie nadruk
# bezpiecznika, a nie superkondensator.
CAPACITANCE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([pnuµm])F\b")
RESISTANCE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kKM]?)\s*(?:Ω|ohm)", re.I)
RESISTOR_POWER = re.compile(r"^Rezystor\b.*?\b(1/8|1/4|1/2|0[.,]25|0[.,]5|[1-9])\s?W\b")
RESISTANCE_CODE = re.compile(r"\b(\d+)([kKMR])(\d+)\b")


def package(text: str) -> tuple[str | None, str | None]:
    """(obudowa, montaż) z tekstu albo (None, None)."""
    for pattern, label, mount in PACKAGES:
        found = re.search(pattern, text, re.I)
        if not found:
            continue
        if "{mm}" in label:
            mm = next(group for group in found.groups() if group)
            return label.format(mm=mm), mount
        return found.expand(label).upper(), mount
    return None, None


def mounting(name: str, description: str, mount_from_package: str | None) -> str | None:
    """Obudowa wygrywa, potem słowo w nazwie, dopiero potem w opisie.

    Nazwa „Kondensator ceramiczny 100 nF THT” z opisem „MLCC …” to część
    przewlekana: nazwę ustala się świadomie, opis bywa przepisem z katalogu.
    """
    if mount_from_package:
        return mount_from_package
    for text in (name, description):
        tht, smd = THT_WORDS.search(text), SMD_WORDS.search(text)
        if tht and not smd:
            return "THT"
        if smd and not tht:
            return "SMD"
    return None


def value(name: str) -> float | None:
    """Wartość w jednostkach podstawowych (F albo Ω) — do sortowania.

    Tylko z nazwy, bo opis zestawu wymienia kilkanaście wartości naraz
    i żadna z nich nie jest „wartością” pudełka.
    """
    found = CAPACITANCE.search(name)
    if found:
        return float(found.group(1).replace(",", ".")) * PREFIX[found.group(2)]
    found = RESISTANCE.search(name)
    if found:
        return float(found.group(1).replace(",", ".")) * PREFIX[found.group(2)]
    found = RESISTANCE_CODE.search(name)
    if found:
        whole, unit, fraction = found.groups()
        scale = 1.0 if unit == "R" else PREFIX[unit]
        return float(f"{whole}.{fraction}") * scale
    return None


def facets(
    name: str, description: str = "", footprint: str = "", tags: str = ""
) -> dict[str, object]:
    """`{"m": "SMD"|"THT"|None, "pk": "0805"|…|None, "v": float|None}`.

    Co wpisał człowiek, wygrywa z odczytem z nazwy: pole `footprint`
    (obudowa) i tag `SMD`/`THT` ustawiane z karty części w Warsztacie.
    """
    pkg, mount = package(footprint) if footprint else (None, None)
    if footprint and pkg is None:
        # Obudowa spoza znanej listy („THT 0207”) — zostaje dosłownie.
        pkg = footprint.strip()
    if pkg is None:
        # Nazwa przed opisem: opis zestawu potrafi wymienić kilka obudów.
        pkg, mount = package(name)
    if pkg is None:
        pkg, mount = package(description)
    if pkg is None:
        # Przy rezystorze przewlekanym „rozmiar” to moc: 1/4 W i 2 W to dwa
        # różne korpusy, a obudowy w nazwie zwykle nie ma.
        power = RESISTOR_POWER.search(name)
        if power:
            pkg = f"{power.group(1).replace(',', '.')} W"
    tagged = {t.strip().upper() for t in (tags or "").split(",")} & {"SMD", "THT"}
    if len(tagged) == 1:
        mount = tagged.pop()
    else:
        mount = mounting(name, description, mount)
    return {"m": mount, "pk": pkg, "v": value(name)}
