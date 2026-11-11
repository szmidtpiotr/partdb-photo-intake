"""Zapisy z karty części Warsztatu (ADR-022, etap 2).

Każda operacja to jedna czynność człowieka przy regale: popraw nazwę, wziąłem
jedną sztukę, włożyłem do pudełka, to nie jest SMD. Dlatego funkcje są małe
i każda kończy się świeżym wierszem i kartą tej jednej części — telefon
podmienia je u siebie bez pobierania całego zrzutu od nowa.

Granice, wszystkie celowe:

* **Wszystko przez `partdb_client`** (02-architektura, reguła 4). Ten moduł
  nie zna adresów REST Part-DB poza nazwami zasobów podawanymi klientowi.
* **Pudełko zakłada człowiek, nie agent** (ADR-022 doprecyzowuje ADR-017):
  agent nadal nie ma narzędzia, które tworzy lokalizację.
* **Montaż i „datasheet nie dotyczy” to tagi części.** Part-DB nie ma na nie
  pól, a tagi są widoczne i edytowalne w jego UI, więc nie powstaje druga
  prawda poza Part-DB (ADR-004).
* **Ilość w kilku pudełkach naraz nie jest zgadywana.** Gdy część leży
  w dwóch miejscach, „ustaw 10 sztuk” nie mówi, w którym — odmawiamy
  z wyjaśnieniem zamiast rozdzielać liczbę po cichu.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .attachments import PICTURE_TYPE_NAMES, resolve_type
from .digest import short_location
from .partdb_client import PartDbClient
from .sources import SourceError, SourceRecorder
from .warsztat import NO_DATASHEET_TAG, PARAMS_NAME

JsonDict = Mapping[str, Any]

MOUNT_TAGS = {"SMD", "THT"}
LEVEL_NAMES = ("Karton", "Kuweta", "Przegroda")


class EditError(RuntimeError):
    """Zmiana jest niewykonalna albo niejednoznaczna — powód w treści."""


def _iri(resource: str, entity_id: int | str) -> str:
    # Adres zasobu buduje klient — jedyne miejsce, które zna ścieżki REST
    # Part-DB (reguła 4 z 02-architektura).
    return PartDbClient.iri(resource, entity_id)


def _id(entity: Any) -> int | None:
    if entity is None:
        return None
    if isinstance(entity, Mapping):
        if entity.get("id") is not None:
            return int(entity["id"])
        entity = entity.get("@id")
    tail = str(entity or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def split_tags(raw: str) -> list[str]:
    return [tag.strip() for tag in (raw or "").split(",") if tag.strip()]


def join_tags(tags: list[str]) -> str:
    seen: list[str] = []
    for tag in tags:
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen)


# --- pola tekstowe i znaczniki ---------------------------------------------


async def update_fields(client: Any, part_id: int, changes: Mapping[str, Any]) -> None:
    """Nazwa, opis, uwagi, kategoria, „sprawdzone”, dane techniczne, linki."""
    part = await client.get_part(part_id)
    iri = _iri("parts", part_id)
    patch: dict[str, Any] = {}

    if "name" in changes:
        name = str(changes["name"] or "").strip()
        if not name:
            raise EditError("Nazwa nie może być pusta.")
        patch["name"] = name
    if "what" in changes:
        patch["description"] = str(changes["what"] or "").strip()
    if "notes" in changes:
        patch["comment"] = str(changes["notes"] or "").strip()
    if "checked" in changes:
        patch["needs_review"] = not bool(changes["checked"])
    if "category" in changes:
        patch["category"] = _iri("categories", int(changes["category"]))
    if "mpn" in changes:
        patch["manufacturer_product_number"] = str(changes["mpn"] or "").strip()
    if "manufacturer" in changes:
        patch["manufacturer"] = await _named(
            client, "manufacturers", str(changes["manufacturer"] or "")
        )
    if "package" in changes:
        patch["footprint"] = await _named(
            client, "footprints", str(changes["package"] or "")
        )

    tags = split_tags(part.get("tags") or "")
    if "mount" in changes:
        tags = [t for t in tags if t.upper() not in MOUNT_TAGS]
        mount = str(changes["mount"] or "").upper()
        if mount in MOUNT_TAGS:
            tags.append(mount)
    if "datasheet_na" in changes:
        tags = [t for t in tags if t != NO_DATASHEET_TAG]
        if changes["datasheet_na"]:
            tags.append(NO_DATASHEET_TAG)
    new_tags = join_tags(tags)
    if new_tags != (part.get("tags") or ""):
        patch["tags"] = new_tags

    if patch:
        await client.patch(iri, patch)

    if "params" in changes:
        await _set_params(client, part, str(changes["params"] or "").strip())
    for kind in ("datasheet_url", "shop_url"):
        if changes.get(kind):
            await add_link(client, part_id, str(changes[kind]), kind)


async def _named(client: Any, resource: str, name: str) -> str | None:
    """IRI producenta albo obudowy o tej nazwie; utwórz, gdy nie ma.

    To są słowniki opisowe (jak typ załącznika), nie pojemniki w warsztacie,
    więc założenie brakującego wpisu niczego nie wymyśla. Pusta nazwa czyści
    pole.
    """
    name = name.strip()
    if not name:
        return None
    for item in await client.list_all(resource):
        if (item.get("name") or "").strip().casefold() == name.casefold():
            return str(item["@id"])
    created = await client.create(resource, {"name": name})
    return str(created["@id"])


async def _set_params(client: Any, part: JsonDict, text: str) -> None:
    """Wolny tekst „50 V, X7R” jako jeden parametr `Parametry`."""
    existing = None
    for ref in part.get("parameters") or []:
        detail = await client.get(str(ref.get("@id")))
        if (detail.get("name") or "") == PARAMS_NAME:
            existing = detail
            break
    if existing and not text:
        await client.delete(str(existing["@id"]))
    elif existing:
        await client.patch(str(existing["@id"]), {"value_text": text})
    elif text:
        await client.create_parameter(
            {
                "name": PARAMS_NAME,
                "value_text": text,
                "element": _iri("parts", part["id"]),
            }
        )


def _shop_label(url: str) -> str:
    """`https://botland.com.pl/…` → `Botland`. Nazwa sklepu z adresu."""
    # Bez urllib: biblioteki HTTP/URL żyją tylko w kliencie (test_layering).
    found = re.match(r"^https?://([^/:?#]+)", url)
    host = found.group(1).lower() if found else ""
    host = re.sub(r"^(www|m)\.", "", host)
    label = host.split(".")[0] if host else "Sklep"
    return label[:1].upper() + label[1:] if label else "Sklep"


async def add_link(client: Any, part_id: int, url: str, kind: str) -> None:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise EditError("Link musi zaczynać się od http:// albo https://.")
    recorder = SourceRecorder(client)
    shop = _shop_label(url)
    try:
        if kind == "datasheet_url":
            plan = await recorder.plan(str(part_id), shop, datasheet_url=url)
        else:
            plan = await recorder.plan(str(part_id), shop, product_url=url)
        if plan.empty:
            raise EditError("Ten link już jest przy części.")
        await recorder.execute(plan)
    except SourceError as exc:
        raise EditError(str(exc)) from exc
    if kind == "datasheet_url":
        # Dokumentacja jest, więc „nie dotyczy” przestaje być prawdą.
        await update_fields(client, part_id, {"datasheet_na": False})


# --- ilość ------------------------------------------------------------------


def _lots(part: JsonDict) -> list[JsonDict]:
    return list(part.get("partLots") or [])


async def set_quantity(
    client: Any, part_id: int, kind: str, amount: float | None, low: bool
) -> None:
    """Postać ilości (ADR-007) zapisana w `PartLot`."""
    if kind not in {"exact", "approx", "some"}:
        raise EditError(f"Nieznana postać ilości: {kind!r}.")
    if kind != "some" and (amount is None or amount < 0):
        raise EditError("Podaj liczbę sztuk (0 albo więcej).")
    payload = {
        "amount": 0 if kind == "some" else float(amount or 0),
        "instock_unknown": kind != "exact",
        "needs_refill": bool(low),
    }
    part = await client.get_part(part_id)
    lots = _lots(part)
    if not lots:
        await client.create_part_lot({**payload, "part": _iri("parts", part_id)})
        return
    if len(lots) > 1:
        # Ilość z kilku pudełek naraz nie daje się rozdzielić uczciwie.
        raise EditError(
            "Ta część leży w kilku pudełkach — zmień ilość osobno w każdym z nich."
        )
    await client.update_part_lot(_id(lots[0]), payload)


async def take_one(client: Any, part_id: int) -> None:
    part = await client.get_part(part_id)
    lots = [lot for lot in _lots(part) if not lot.get("instock_unknown")]
    if not lots:
        raise EditError("„−1” działa tylko przy ilości liczonej co do sztuki.")
    lot = max(lots, key=lambda item: float(item.get("amount") or 0))
    left = float(lot.get("amount") or 0)
    if left <= 0:
        raise EditError("Stan już jest zero.")
    await client.update_part_lot(_id(lot), {"amount": left - 1})


# --- pudełka -------------------------------------------------------------------


async def assign_box(
    client: Any, part_id: int, box_id: int, from_box: int | None = None
) -> None:
    """Włóż do pudełka. `from_box` = przenieś stamtąd.

    Kolejność wyboru stanu: wskazany (przeniesienie) → leżący luzem →
    nowy stan w tym pudełku z ilością nieznaną (część leży w dwóch miejscach).
    """
    part = await client.get_part(part_id)
    lots = _lots(part)
    target = _iri("storage_locations", box_id)
    if any(_id(lot.get("storage_location")) == box_id for lot in lots):
        raise EditError("Ta część już jest w tym pudełku.")
    chosen = None
    if from_box is not None:
        chosen = next(
            (lot for lot in lots if _id(lot.get("storage_location")) == from_box), None
        )
        if chosen is None:
            raise EditError("Nie ma tej części we wskazanym pudełku.")
    else:
        chosen = next((lot for lot in lots if not lot.get("storage_location")), None)
    if chosen is not None:
        await client.update_part_lot(_id(chosen), {"storage_location": target})
        return
    await client.create_part_lot(
        {
            "part": _iri("parts", part_id),
            "storage_location": target,
            "amount": 0,
            "instock_unknown": True,
        }
    )


async def remove_from_box(client: Any, part_id: int, box_id: int) -> None:
    """Wyjmij z pudełka. Ostatni stan zostaje (luzem), kolejne znikają.

    Skasowanie jedynego stanu zamieniłoby część w wpis katalogowy — „nie
    mam” — a ona nadal leży na stole, tylko bez pudełka.
    """
    part = await client.get_part(part_id)
    lots = _lots(part)
    lot = next(
        (lot for lot in lots if _id(lot.get("storage_location")) == box_id), None
    )
    if lot is None:
        raise EditError("Nie ma tej części w tym pudełku.")
    if len(lots) == 1:
        await client.update_part_lot(_id(lot), {"storage_location": None})
    else:
        await client.delete(str(lot.get("@id") or _iri("part_lots", _id(lot))))


def next_box_name(locations: list[JsonDict], parent_id: int | None) -> tuple[str, str]:
    """(nazwa, kod) kolejnego pudełka: Karton A → Kuweta A1 → Przegroda 01."""
    by_id = {_id(loc): loc for loc in locations}
    codes = {
        short_location(loc.get("full_path") or loc.get("name") or "")
        for loc in locations
    }
    if parent_id is None:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if letter not in codes:
                return f"Karton {letter}", letter
        raise EditError("Skończyły się litery kartonów.")
    parent = by_id.get(parent_id)
    if parent is None:
        raise EditError("Nie ma takiego pudełka nadrzędnego.")
    parent_code = short_location(parent.get("full_path") or parent.get("name") or "")
    depth = len(parent_code.split("/"))
    if depth >= len(LEVEL_NAMES):
        raise EditError("Przegroda to najniższy poziom — głębiej się nie da.")
    letter = parent_code.split("/")[0]
    for n in range(1, 100):
        token = f"{letter}{n}" if depth == 1 else f"{n:02d}"
        if f"{parent_code}/{token}" not in codes:
            return f"{LEVEL_NAMES[depth]} {token}", f"{parent_code}/{token}"
    raise EditError("Za dużo pudełek na jednym poziomie.")


async def create_box(client: Any, parent_id: int | None, name: str = "") -> JsonDict:
    locations = await client.list_storage_locations()
    default_name, _code = next_box_name(locations, parent_id)
    payload: dict[str, Any] = {"name": (name or "").strip() or default_name}
    if parent_id is not None:
        payload["parent"] = _iri("storage_locations", parent_id)
    return await client.create("storage_locations", payload)


# --- zdjęcia ---------------------------------------------------------------------


async def add_photo(client: Any, part_id: int, url: str, name: str) -> None:
    """Dopnij zdjęcie leżące u nas (adres, nie kopia — ADR-020/022)."""
    await client.create_attachment(
        {
            "name": name,
            "attachment_type": await resolve_type(
                client, PICTURE_TYPE_NAMES, "Zdjęcie"
            ),
            "element": _iri("parts", part_id),
            "url": url,
        }
    )
    part = await client.get_part(part_id)
    if not part.get("master_picture_attachment"):
        attachments = list(part.get("attachments") or [])
        if attachments:
            await set_main_photo(client, part_id, _id(attachments[-1]))


async def set_main_photo(client: Any, part_id: int, attachment_id: int | None) -> None:
    await client.patch(
        _iri("parts", part_id),
        {"master_picture_attachment": _iri("attachments", attachment_id)},
    )


async def remove_photo(client: Any, part_id: int, attachment_id: int) -> None:
    part = await client.get_part(part_id)
    ids = {_id(a) for a in part.get("attachments") or []}
    if attachment_id not in ids:
        raise EditError("To zdjęcie nie należy do tej części.")
    await client.delete(_iri("attachments", attachment_id))


async def delete_part(client: Any, part_id: int, confirm_name: str) -> None:
    part = await client.get_part(part_id)
    if (confirm_name or "").strip().casefold() != (
        part.get("name") or ""
    ).strip().casefold():
        raise EditError("Potwierdzenie nie zgadza się z nazwą części.")
    await client.delete(_iri("parts", part_id))
