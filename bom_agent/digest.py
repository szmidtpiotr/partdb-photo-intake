"""Zwięzły zrzut stanu magazynu — format z `docs/04-model-danych.md`.

Cel: cały magazyn w kilku kB tekstu, żeby agent projektujący układ miał go
w kontekście od razu, zamiast zadawać dwadzieścia pytań (ADR-016).

Trzy rozstrzygnięcia, które przesądzają o tym, czy zrzut mówi prawdę:

1. **Wpis katalogowy to nie zawartość kartonu.** Części zaciągnięte
   automatycznie od dostawcy nie mają żadnego `PartLot`, więc nie mają ani
   stanu, ani lokalizacji. Idą do osobnej sekcji na końcu.
2. **Dyskryminatorem jest `partLots == []`, nie `total_instock == 0`.**
   Tę samą zerową wartość ma realna część, która się skończyła — a to inna
   odpowiedź na pytanie „czy mam". Taka pozycja zostaje w swojej kategorii
   z adnotacją `brak`.
3. **Klasa B z ADR-007** (`instock_unknown = true`) renderuje się jako `~N`.
   Udawanie, że 200 rezystorów policzono co do sztuki, byłoby gorsze niż
   jawne przybliżenie.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .quantity import UNCOUNTED

JsonDict = Mapping[str, Any]

NO_CATEGORY = "BEZ KATEGORII"
CATALOG_HEADER = "ZNANE, ALE NIE NA STANIE  (wpisy katalogowe — brak lokalizacji)"
# Stan bez pojemnika renderuje się pustą kolumną lokalizacji — nagłówek
# sekcji mówi już, że te rzeczy leżą luzem, a „?" w każdym wierszu byłby
# szumem sugerującym brakującą daną zamiast świadomego stanu (ADR-017).
UNASSIGNED = ""
UNASSIGNED_HEADER = "MAM, ALE BEZ PRZYPISANIA  (leży luzem — do rozłożenia)"

# Kolumna z nazwą dopasowuje się do najdłuższej nazwy w zrzucie, żeby ilości
# stały w jednej linii pionowej. Sufit chroni przed jedną patologiczną nazwą,
# która zepchnęłaby ilości poza czytelną szerokość; nazw nie skracamy, bo to
# po nich człowiek rozpoznaje część.
MIN_NAME_WIDTH = 24
MAX_NAME_WIDTH = 44
AMOUNT_WIDTH = 7


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Polska odmiana liczebnika: 1 wpis, 3 wpisy, 5 wpisów.

    Zrzut czyta zarówno agent, jak i człowiek — „4 wpisów" wygląda na błąd
    w danych, a nie na niedbałość w tekście.
    """
    if count == 1:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def short_location(full_path: str) -> str:
    """`"Karton A → Kuweta A1"` → `"A/A1"`.

    Skrót bierze ostatni człon każdego poziomu, bo taksonomia z
    `04-model-danych.md` koduje pozycję właśnie w nim („Kuweta **A1**").
    Gdy człon nie wygląda na kod (jest długi albo wieloznaczny), zostaje
    cała nazwa poziomu — czytelność wygrywa ze zwięzłością.
    """
    if not full_path:
        return "?"
    parts = []
    for level in full_path.split("→"):
        level = level.strip()
        if not level:
            continue
        token = level.split()[-1]
        parts.append(token if len(token) <= 4 else level)
    return "/".join(parts) if parts else "?"


def _lot_location(lot: JsonDict) -> str:
    location = lot.get("storage_location") or {}
    path = location.get("full_path") or location.get("name") or ""
    return short_location(path) if path else UNASSIGNED


def _has_any_location(part: JsonDict) -> bool:
    return any(
        (lot.get("storage_location") or {}).get("full_path")
        or (lot.get("storage_location") or {}).get("name")
        for lot in part.get("partLots") or []
    )


def _category_of(part: JsonDict) -> str:
    category = part.get("category") or {}
    name = category.get("full_path") or category.get("name") or ""
    return name.strip() or NO_CATEGORY


def _amount(part: JsonDict) -> tuple[str, bool]:
    """Zwróć tekst ilości i informację, czy pozycja jest przybliżona."""
    lots = part.get("partLots") or []
    approximate = any(lot.get("instock_unknown") for lot in lots)
    total = part.get("total_instock")
    if total is None:
        total = sum(lot.get("amount") or 0 for lot in lots)
    total = int(total)

    if total == 0 and not approximate:
        return "brak", False
    if approximate:
        # Stan klasy B bez oszacowania (`amount = 0`, `instock_unknown`) to
        # „jest, nie liczone" — tak wygląda segregator-próbnik. Renderowanie
        # tego jako `~0` czytało się dokładnie odwrotnie do prawdy: jak
        # pozycja, której nie ma.
        return (UNCOUNTED if total == 0 else f"~{total}"), True
    return f"x{total}", False


def _flags(part: JsonDict) -> list[str]:
    lots = part.get("partLots") or []
    flags = []
    if any(lot.get("needs_refill") for lot in lots):
        flags.append("[kończy się]")
    if part.get("needs_review"):
        flags.append("[do-sprawdzenia]")
    return flags


def _name_of(part: JsonDict) -> str:
    return (part.get("name") or "bez nazwy").strip()


def _name_width(parts: Iterable[JsonDict]) -> int:
    longest = max((len(_name_of(part)) for part in parts), default=0)
    return max(MIN_NAME_WIDTH, min(longest, MAX_NAME_WIDTH))


def _line(part: JsonDict, width: int) -> str:
    amount, _ = _amount(part)
    locations = ", ".join(
        dict.fromkeys(_lot_location(lot) for lot in part.get("partLots") or [])
    )
    cells = [f"  {_name_of(part):<{width}}", f"{amount:<{AMOUNT_WIDTH}}", locations]
    return " ".join(cell for cell in cells if cell.strip()).rstrip()


def _normalized(text: str) -> str:
    """Nazwa sprowadzona do porównywalnej postaci: bez `[LCSC]`, bez spacji."""
    text = text.lower()
    if "[" in text:
        text = text.split("[", 1)[0]
    return "".join(char for char in text if char.isalnum())


def _stock_twin(part: JsonDict, in_stock: Iterable[JsonDict]) -> str | None:
    """Znajdź pozycję na stanie, która wygląda na tę samą część.

    Bez tego wpis katalogowy „BME280 [LCSC]" wygląda na coś, czego nie mam,
    choć trzy sztuki leżą w kuwecie A2 pod nazwą „BME280". Pełna
    deduplikacja to zadanie z M2 — tu wystarczy nie wprowadzać w błąd.
    """
    keys = {_normalized(_name_of(part))}
    mpn = (part.get("manufacturer_product_number") or "").strip()
    if mpn:
        keys.add(_normalized(mpn))
    keys.discard("")

    for candidate in in_stock:
        candidate_keys = {_normalized(_name_of(candidate))}
        candidate_mpn = (candidate.get("manufacturer_product_number") or "").strip()
        if candidate_mpn:
            candidate_keys.add(_normalized(candidate_mpn))
        if keys & candidate_keys:
            return _name_of(candidate)
    return None


def _catalog_line(part: JsonDict, width: int, twin: str | None) -> str:
    mpn = (part.get("manufacturer_product_number") or "").strip()
    line = f"  {_name_of(part):<{width}} {mpn}".rstrip()
    if twin:
        line += f"   <- to samo co: {twin} (na stanie)"
    return line


def _matches_scope(part: JsonDict, scope: str) -> bool:
    haystack = [
        part.get("name") or "",
        part.get("description") or "",
        _category_of(part),
    ]
    haystack += [_lot_location(lot) for lot in part.get("partLots") or []]
    return any(scope in value.lower() for value in haystack)


def build_digest(
    parts: Iterable[JsonDict],
    *,
    now: datetime | None = None,
    scope: str | None = None,
) -> str:
    """Zbuduj tekstowy zrzut stanu z surowych części Part-DB.

    Funkcja jest czysta — bierze dane, oddaje tekst. Pobieraniem zajmuje się
    `inventory_digest()`, dzięki czemu format da się testować bez sieci.
    """
    parts = list(parts)
    if scope:
        needle = scope.strip().lower()
        parts = [part for part in parts if _matches_scope(part, needle)]

    with_lots = [part for part in parts if part.get("partLots")]
    catalog = [part for part in parts if not part.get("partLots")]
    # Część ze stanem, ale bez pojemnika, to inna odpowiedź niż wpis
    # katalogowy: mam ją fizycznie, tylko jeszcze nie wiem gdzie leży
    # (ADR-017). Idzie do osobnej sekcji, bo to jest lista robocza
    # na sesję sortowania.
    in_stock = [part for part in with_lots if _has_any_location(part)]
    unassigned = [part for part in with_lots if not _has_any_location(part)]

    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    header = f"=== MAGAZYN — stan na {stamp} ==="
    if scope:
        header += f"\n(zakres: {scope})"

    lines = [header, ""]
    width = _name_width(parts)

    by_category: dict[str, list[JsonDict]] = {}
    for part in in_stock:
        by_category.setdefault(_category_of(part), []).append(part)

    for category in sorted(by_category, key=str.casefold):
        lines.append(category.upper())
        for part in sorted(
            by_category[category], key=lambda p: (p.get("name") or "").casefold()
        ):
            line = _line(part, width)
            flags = _flags(part)
            lines.append(f"{line} {' '.join(flags)}".rstrip() if flags else line)
        lines.append("")

    if unassigned:
        lines.append(UNASSIGNED_HEADER)
        for part in sorted(unassigned, key=lambda p: (p.get("name") or "").casefold()):
            line = _line(part, width)
            flags = _flags(part)
            lines.append(f"{line} {' '.join(flags)}".rstrip() if flags else line)
        lines.append("")

    if catalog:
        lines.append(CATALOG_HEADER)
        for part in sorted(catalog, key=lambda p: (p.get("name") or "").casefold()):
            twin = _stock_twin(part, in_stock + unassigned)
            lines.append(_catalog_line(part, width, twin))
        lines.append("")

    locations = {
        _lot_location(lot) for part in in_stock for lot in part.get("partLots") or []
    }
    approximate = sum(1 for part in with_lots if _amount(part)[1])
    empty = sum(1 for part in with_lots if _amount(part)[0] == "brak")

    total = "RAZEM: {stock} {stock_word} na stanie w {loc} {loc_word}".format(
        stock=len(in_stock),
        stock_word=_plural(len(in_stock), "pozycja", "pozycje", "pozycji"),
        loc=len(locations),
        loc_word=_plural(
            len(locations), "lokalizacji", "lokalizacjach", "lokalizacjach"
        ),
    )
    if unassigned:
        total += ", {n} {word} bez przypisanego pojemnika".format(
            n=len(unassigned),
            word=_plural(len(unassigned), "pozycja", "pozycje", "pozycji"),
        )
    if catalog:
        total += ", {cat} {cat_word} bez stanu".format(
            cat=len(catalog),
            cat_word=_plural(
                len(catalog),
                "wpis katalogowy",
                "wpisy katalogowe",
                "wpisów katalogowych",
            ),
        )
    summary = [f"{total}."]
    if approximate:
        summary.append(
            f"Ilości z `~` ({approximate} poz.) są przybliżone — klasa B "
            "wg ADR-007, liczona kubełkowo, nie co do sztuki. "
            f"`{UNCOUNTED}` znaczy: mam, ale nawet kubełka nie oszacowałem."
        )
    if empty:
        summary.append(
            f"`brak` ({empty} poz.) oznacza część, która JEST w magazynie, "
            "ale skończyła się — inaczej niż wpis katalogowy."
        )
    if unassigned:
        summary.append(
            f"Sekcja bez przypisania ({len(unassigned)} poz.) to lista robocza "
            "na sesję sortowania — te rzeczy masz, ale nie mają jeszcze pudła."
        )
    if not with_lots and not catalog:
        summary = ["Magazyn pusty albo zakres nic nie objął."]

    lines.extend(summary)
    return "\n".join(lines).rstrip() + "\n"


async def inventory_digest(client: Any, scope: str | None = None) -> str:
    """Pobierz stan z Part-DB i zbuduj zrzut.

    Jedno zapytanie na całą kolekcję — `partLots` i `total_instock` są już
    w wyniku listowania części, więc stan i lokalizacja nie wymagają
    dopytywania po części (docs/05-mcp-tools.md).
    """
    parts = await client.list_parts()
    return build_digest(parts, scope=scope)
