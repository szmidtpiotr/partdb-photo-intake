"""Dane dla frontu „Warsztat” (ADR-022): zrzut magazynu i karta części.

Front szuka w przeglądarce, więc dostaje cały magazyn naraz, w postaci
okrojonej do tego, co pokazuje wiersz listy. Przy 570 częściach to kilkadziesiąt
kilobajtów — mniej niż jedno zdjęcie — a szukanie w trakcie pisania nie
czeka na serwer po każdej literze.

Szczegóły (opis, uwagi, załączniki, parametry, sklep) przychodzą dopiero
przy otwarciu karty, bo zebranie ich dla wszystkich części naraz to kilkaset
dodatkowych żądań do Part-DB.

Trzy tłumaczenia, które muszą być wszędzie takie same:

* **Ilość** z `PartLot`: `instock_unknown=false` → dokładna, `true` z liczbą
  → przybliżona (kubełek z ADR-007), `true` bez liczby → „jest”. Część bez
  żadnego stanu to wpis katalogowy — „nie mam”, a nie „zero”.
* **Kod pudełka** to `short_location` ścieżki („Karton A → Kuweta A3” →
  `A/A3`), ten sam skrót, którego używa zrzut dla agenta.
* **Adres zdjęcia**: zdjęcia z intake'u mają w Part-DB nasz publiczny adres,
  więc oddajemy go jako względny (działa i pod LAN-owym adresem), a pliki
  trzymane w Part-DB idą przez wąskie przejście `/api/warsztat/media`.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .attachments import DATASHEET_TYPE_NAMES, PICTURE_TYPE_NAMES
from .digest import short_location
from .facets import facets

# Tag „datasheet nie dotyczy” ustawiany z karty (warsztat_edit).
NO_DATASHEET_TAG = "datasheet-nie-dotyczy"
PARAMS_NAME = "Parametry"

JsonDict = Mapping[str, Any]

# Szerokość miniatury w wierszu listy i zdjęcia na karcie. Miniatura ma
# 64 px na ekranie, więc 160 px wystarcza z zapasem na ekran o gęstości ×2,5;
# oryginał z telefonu (4 MB) w liście 400 wierszy położyłby każdy telefon.
THUMB_EDGE = 160
CARD_EDGE = 1024

# Kategorie, przy których datasheet nie istnieje. Przycisk „poszukaj w sieci"
# przy przewodach Dupont byłby obietnicą bez pokrycia.
NO_DATASHEET_CATEGORIES = {
    "przewody i złącza",
    "elementy mechaniczne",
    "płytki prototypowe",
    "narzędzia pomiarowe",
    "narzędzia i drobne",
    "radiatory",
}


def _id(entity: JsonDict | str | None) -> int | None:
    """`{"@id": "/api/x/12"}` albo `"/api/x/12"` → 12."""
    if entity is None:
        return None
    if isinstance(entity, Mapping):
        if entity.get("id") is not None:
            return int(entity["id"])
        entity = entity.get("@id")
    if not entity:
        return None
    tail = str(entity).rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def media_url(
    raw: str | None, public_base_url: str, edge: int | None = None
) -> str | None:
    """Adres pliku tak, żeby przeglądarka telefonu go otworzyła."""
    if not raw:
        return None
    url = str(raw)
    base = public_base_url.rstrip("/")
    if base and url.startswith(base + "/"):
        url = url[len(base) :]
    if url.startswith(("/api/intake/", "/api/warsztat/photo/")):
        if edge:
            url += ("&" if "?" in url else "?") + f"w={edge}"
        return url
    if url.startswith("/media/"):
        # Nazwy plików w Part-DB są już „ślimakami” (bez spacji i ogonków),
        # więc adres nie wymaga kodowania.
        return f"/api/warsztat/media?path={url}"
    return url


def quantity(lots: list[JsonDict]) -> dict[str, Any]:
    """Ilość w jednej z trzech postaci, plus „nie mam” dla wpisu katalogowego."""
    if not lots:
        return {"k": "none", "n": None}
    total = sum(float(lot.get("amount") or 0) for lot in lots)
    number = int(total) if total == int(total) else total
    if any(lot.get("instock_unknown") for lot in lots):
        return {"k": "approx", "n": number} if total > 0 else {"k": "some", "n": None}
    return {"k": "exact", "n": number}


def _is_datasheet(attachment: JsonDict, type_names: Mapping[str, str]) -> bool:
    kind = type_names.get(str(attachment.get("attachment_type") or ""), "")
    if kind:
        return kind in DATASHEET_TYPE_NAMES
    # Lista części nie niesie typu załącznika — zostaje nazwa i rozszerzenie.
    name = (attachment.get("name") or "").casefold()
    path = (
        attachment.get("media_url") or attachment.get("external_path") or ""
    ).casefold()
    return name.startswith("datasheet") or path.endswith(".pdf")


def _is_picture(attachment: JsonDict, type_names: Mapping[str, str]) -> bool:
    kind = type_names.get(str(attachment.get("attachment_type") or ""), "")
    if kind:
        return kind in PICTURE_TYPE_NAMES
    if attachment.get("picture"):
        return True
    path = (
        attachment.get("media_url") or attachment.get("external_path") or ""
    ).casefold()
    return "/api/intake/" in path or path.rsplit("?", 1)[0].endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    )


def _search_text(part: JsonDict) -> str:
    """Tekst, po którym szuka przeglądarka, poza nazwą i kategorią."""
    bits = [
        part.get("description") or "",
        part.get("manufacturer_product_number") or "",
        part.get("tags") or "",
    ]
    return " ".join(bit for bit in bits if bit)[:400]


def build_boxes(
    locations: list[JsonDict], public_base_url: str
) -> list[dict[str, Any]]:
    boxes = []
    for location in locations:
        path = location.get("full_path") or location.get("name") or ""
        picture = location.get("master_picture_attachment") or {}
        boxes.append(
            {
                "id": _id(location),
                "code": short_location(path),
                "name": location.get("name") or "",
                "path": " › ".join(level.strip() for level in path.split("→")),
                "parent": _id(location.get("parent")),
                "photo": media_url(
                    picture.get("media_url"), public_base_url, THUMB_EDGE
                ),
            }
        )
    return sorted(boxes, key=lambda box: box["code"])


def part_row(part: JsonDict, public_base_url: str) -> dict[str, Any]:
    """Jedna część w postaci wiersza listy. Ten sam kształt w zrzucie i w
    odpowiedzi na zapis z karty — telefon podmienia wiersz bez pobierania
    całego magazynu."""
    lots = list(part.get("partLots") or [])
    locs = []
    for lot in lots:
        box = _id(lot.get("storage_location"))
        if box is None:
            continue
        exact = not lot.get("instock_unknown")
        locs.append({"box": box, "n": lot.get("amount") if exact else None})
    picture = part.get("master_picture_attachment") or {}
    attachments = list(part.get("attachments") or [])
    tags = part.get("tags") or ""
    return {
        "id": part.get("id"),
        "name": part.get("name") or "",
        "cat": _id(part.get("category")),
        "q": quantity(lots),
        "low": any(lot.get("needs_refill") for lot in lots),
        "locs": locs,
        "thumb": media_url(picture.get("media_url"), public_base_url, THUMB_EDGE),
        "chk": bool(part.get("needs_review")),
        "ds": any(_is_datasheet(a, {}) for a in attachments),
        # Datasheet nie istnieje (przewody, radiatory albo oznaczone ręcznie)
        # — taka część nie trafia na listę „Bez datasheetu”.
        "dsn": (
            ((part.get("category") or {}).get("name") or "").casefold()
            in NO_DATASHEET_CATEGORIES
            or NO_DATASHEET_TAG in [t.strip() for t in tags.split(",")]
        ),
        "s": _search_text(part),
        # Montaż (SMD/THT), obudowa i wartość do filtrów i sortowania.
        **facets(
            part.get("name") or "",
            part.get("description") or "",
            (part.get("footprint") or {}).get("name") or "",
            tags,
        ),
    }


def build_snapshot(
    parts: list[JsonDict],
    categories: list[JsonDict],
    locations: list[JsonDict],
    public_base_url: str,
) -> dict[str, Any]:
    """Cały magazyn w postaci wiersza listy — patrz docstring modułu."""
    rows = [part_row(part, public_base_url) for part in parts]
    per_category: Counter[int] = Counter(r["cat"] for r in rows if r["cat"] is not None)
    per_box: Counter[int] = Counter(loc["box"] for r in rows for loc in r["locs"])

    cats = [
        {
            "id": _id(category),
            "name": category.get("name") or "",
            "parent": _id(category.get("parent")),
            "n": per_category.get(_id(category) or -1, 0),
        }
        for category in categories
    ]
    # Kategoria nadrzędna liczy też części z podkategorii — tak jak kafelek
    # „Moduły · 33” w projekcie, który obejmuje komunikacyjne i przetwornice.
    children: dict[int | None, list[dict[str, Any]]] = {}
    for cat in cats:
        children.setdefault(cat["parent"], []).append(cat)

    def total(cat: dict[str, Any]) -> int:
        return cat["n"] + sum(total(child) for child in children.get(cat["id"], []))

    for cat in cats:
        cat["total"] = total(cat)
    cats.sort(key=lambda cat: (-cat["total"], cat["name"]))

    boxes = build_boxes(locations, public_base_url)
    for box in boxes:
        box["n"] = per_box.get(box["id"], 0)

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "parts": rows,
        "categories": cats,
        "boxes": boxes,
    }


async def snapshot(client: Any, public_base_url: str) -> dict[str, Any]:
    parts, categories, locations = await asyncio.gather(
        client.list_parts(),
        client.list_categories(),
        client.list_storage_locations(),
    )
    return build_snapshot(parts, categories, locations, public_base_url)


async def _safe_get(client: Any, iri: str) -> JsonDict | None:
    try:
        return await client.get(iri)
    except Exception:  # noqa: BLE001 — brak jednego parametru nie psuje karty
        return None


async def part_card(client: Any, part_id: int, public_base_url: str) -> dict[str, Any]:
    """Wszystko, co pokazuje karta części, poza tym, co jest już w zrzucie."""
    part = await client.get_part(part_id)
    types = {
        str(item.get("@id")): (item.get("name") or "").strip().casefold()
        for item in await client.list_attachment_types()
    }

    attachments = list(part.get("attachments") or [])
    master = _id(part.get("master_picture_attachment"))
    pictures = [a for a in attachments if _is_picture(a, types)]
    # Miniatura wybrana w Part-DB idzie pierwsza — to ją widać w liście,
    # więc karta ma się otworzyć na tym samym obrazie.
    pictures.sort(key=lambda a: 0 if _id(a) == master else 1)
    photos = [
        {
            "id": _id(a),
            "url": media_url(a.get("media_url"), public_base_url, CARD_EDGE),
            "name": a.get("name") or "",
            "main": _id(a) == master,
        }
        for a in pictures
        if a.get("media_url")
    ]
    datasheets = [
        {
            "url": media_url(a.get("media_url"), public_base_url),
            "name": a.get("name") or "",
        }
        for a in attachments
        if _is_datasheet(a, types) and a.get("media_url")
    ]

    details = await asyncio.gather(
        *(_safe_get(client, str(p.get("@id"))) for p in part.get("parameters") or []),
        *(_safe_get(client, str(o.get("@id"))) for o in part.get("orderdetails") or []),
    )
    n_params = len(part.get("parameters") or [])
    parameters = [d for d in details[:n_params] if d]
    orders = [d for d in details[n_params:] if d]

    tech: list[dict[str, str]] = []
    manufacturer = (part.get("manufacturer") or {}).get("name")
    if manufacturer:
        tech.append({"k": "Producent", "v": manufacturer})
    if part.get("manufacturer_product_number"):
        tech.append({"k": "Nr katalogowy", "v": part["manufacturer_product_number"]})
    footprint = (part.get("footprint") or {}).get("name")
    if footprint:
        tech.append({"k": "Obudowa", "v": footprint})
    params_text = ""
    for parameter in parameters:
        value = parameter.get("formatted") or parameter.get("value_text") or ""
        if parameter.get("name") == PARAMS_NAME:
            params_text = str(value)
        if parameter.get("name") and value:
            tech.append({"k": parameter["name"], "v": str(value)})
    tags = [t.strip() for t in (part.get("tags") or "").split(",") if t.strip()]
    row = part_row(part, public_base_url)

    shops = [
        {
            "url": order["supplier_product_url"],
            "name": (order.get("supplier") or {}).get("name") or "Sklep",
        }
        for order in orders
        if order.get("supplier_product_url")
    ]
    if not shops and part.get("manufacturer_product_url"):
        shops.append({"url": part["manufacturer_product_url"], "name": "Producent"})

    category = part.get("category") or {}
    if datasheets:
        datasheet = "file"
    elif row["dsn"]:
        datasheet = "none"
    else:
        datasheet = "search"

    return {
        "id": part.get("id"),
        "name": part.get("name") or "",
        "what": part.get("description") or "",
        "notes": part.get("comment") or "",
        "chk": bool(part.get("needs_review")),
        "cat": _id(category),
        "cat_path": " › ".join(
            level.strip()
            for level in (category.get("full_path") or "").split("→")
            if level.strip()
        ),
        "photos": photos,
        "datasheet": datasheet,
        "datasheets": datasheets,
        "shops": shops,
        "tech": tech,
        # Surowe wartości do arkuszy edycji (etap 2 ADR-022).
        "edit": {
            "manufacturer": manufacturer or "",
            "mpn": part.get("manufacturer_product_number") or "",
            "package": footprint or "",
            "mount": next((t.upper() for t in tags if t.upper() in {"SMD", "THT"}), ""),
            "params": params_text,
            "datasheet_na": NO_DATASHEET_TAG in tags,
        },
        "row": row,
    }
