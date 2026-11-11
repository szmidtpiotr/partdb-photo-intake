"""Wskazywanie pojemnika po nazwie, ścieżce albo kodzie `A/A1`.

Jedno miejsce, bo pytanie „który to pojemnik" pada i przy zatwierdzaniu
intake'u, i przy przypisywaniu rzeczy leżących luzem. Dwie kopie tej
logiki oznaczałyby, że `Kuweta A1` znaczy co innego w każdym narzędziu.

**Lokalizacji nigdy nie tworzymy** (ADR-017). Pojemnik istnieje fizycznie
albo nie istnieje; wymyślony w bazie to część „leżąca" tam, gdzie nikt jej
nie znajdzie.
"""

from __future__ import annotations

from typing import Any

from .digest import short_location

JsonDict = dict[str, Any]


class LocationError(RuntimeError):
    """Nie da się wskazać pojemnika, o który chodzi."""


def location_label(location: JsonDict | None) -> str:
    if not location:
        return "BEZ PRZYPISANIA — do rozłożenia przy sortowaniu"
    return str(location.get("full_path") or location.get("name") or "?")


def match_location(locations: list[JsonDict], wanted: str) -> JsonDict | None:
    target = wanted.strip().casefold()
    for location in locations:
        candidates = {
            (location.get("name") or "").casefold(),
            (location.get("full_path") or "").casefold(),
            short_location(location.get("full_path") or "").casefold(),
        }
        if target in candidates:
            return location
    return None


async def resolve_location(client: Any, wanted: str) -> JsonDict | None:
    """Znajdź lokalizację. Pusta wartość znaczy „jeszcze nieprzypisane".

    `PartLot.storage_location` jest w Part-DB polem opcjonalnym, więc
    „mam, ale nie wiem gdzie" to pełnoprawny stan, a nie brak danych
    (ADR-017). Nieznana nazwa to co innego niż pusta — i kończy się błędem.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        return None

    locations = await client.list_storage_locations()
    found = match_location(locations, wanted)
    if found is not None:
        return found

    if not locations:
        # Stan normalny na początku (ADR-017): pudeł jeszcze nie ma, bo
        # struktura wyłoni się dopiero ze spisanego zbioru. Podpowiedź
        # z dyktowania („kuweta B3") jest wtedy planem, nie faktem.
        raise LocationError(
            f"W Part-DB nie ma jeszcze żadnej lokalizacji, więc {wanted!r} "
            "nie istnieje. Zapisz bez lokalizacji (pusty `location`) — "
            "„mam, ale nie wiem gdzie” to poprawny stan (ADR-017). "
            "Pojemniki zakłada się w UI Part-DB przy sortowaniu."
        )

    known = ", ".join(
        sorted(
            short_location(location.get("full_path") or location.get("name") or "")
            for location in locations
        )
    )
    raise LocationError(
        f"Nie ma lokalizacji {wanted!r}. Istniejące: {known}. "
        "Pojemników nie wymyślamy — nową zakłada się w UI Part-DB."
    )
