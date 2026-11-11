"""Wskazanie części, o które chodzi — wspólne dla operacji masowych.

Przypisywanie pojemnika i uzupełnianie danych od dostawcy zadają to samo
pytanie: *„która to część?"*. Odpowiedź musi być jedna, bo rozjazd oznaczałby,
że jedno narzędzie bierze `Kondensator 100nF 0805`, a drugie `…1206` — przy
tym samym poleceniu użytkownika.

Zasada nadrzędna: **niejednoznaczność zatrzymuje pracę.** Wybranie pierwszej
pasującej pozycji jest gorsze niż niewybranie żadnej, bo błąd wychodzi dopiero
przy budowie układu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .naming import normalize, tokens

JsonDict = dict[str, Any]

# Ilu kandydatów pokazać przy niejednoznaczności. Pełna lista przy „kondensator"
# to osiemdziesiąt linii, po których i tak nikt nie wybiera.
MAX_CANDIDATES = 8


class SelectionError(RuntimeError):
    """Wskazanie części jest niewykonalne — np. nie ma takiej kategorii."""


def name_of(part: JsonDict) -> str:
    return (part.get("name") or "bez nazwy").strip()


def mpn_of(part: JsonDict) -> str:
    return (part.get("manufacturer_product_number") or "").strip()


def category_of(part: JsonDict) -> str:
    category = part.get("category") or {}
    return (category.get("full_path") or category.get("name") or "").strip()


@dataclass
class Selection:
    chosen: list[JsonDict] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)


def select_parts(
    parts: list[JsonDict], wanted: list[str], category: str = ""
) -> Selection:
    """Wybierz części po nazwie, numerze katalogowym, identyfikatorze albo
    całą kategorię naraz."""
    if category:
        needle = normalize(category)
        chosen = [part for part in parts if normalize(category_of(part)) == needle]
        if not chosen:
            known = sorted({category_of(part) for part in parts if category_of(part)})
            raise SelectionError(
                f"Nie ma kategorii {category!r}. Istniejące: "
                f"{', '.join(known) or 'brak'}."
            )
        return Selection(chosen=chosen)

    selection = Selection()
    for identifier in wanted:
        needle = identifier.strip()
        if not needle:
            continue

        exact = [
            part
            for part in parts
            if str(part.get("@id") or "").endswith(f"/{needle.lstrip('/')}")
            or str(part.get("id")) == needle
            or normalize(name_of(part)) == normalize(needle)
            or (mpn_of(part) and normalize(mpn_of(part)) == normalize(needle))
        ]
        if len(exact) == 1:
            selection.chosen.append(exact[0])
            continue
        if len(exact) > 1:
            selection.ambiguous.append((needle, [name_of(part) for part in exact]))
            continue

        # Bez trafienia dokładnego szukamy podobnych, ale ich NIE wybieramy:
        # „kondensator 100n" pasuje do dwudziestu pozycji.
        marks = tokens(needle)
        similar = [
            part
            for part in parts
            if (marks and marks & tokens(name_of(part)))
            or normalize(needle) in normalize(name_of(part))
        ]
        if similar:
            selection.ambiguous.append(
                (needle, [name_of(part) for part in similar[:MAX_CANDIDATES]])
            )
        else:
            selection.unknown.append(needle)

    return selection
