"""Dopinanie plików do części — jedno miejsce dla wszystkich źródeł.

Zdjęcie z intake'u, zdjęcie katalogowe od dostawcy i zdjęcie ze sklepu
trafiają do Part-DB tą samą drogą i różnią się wyłącznie tym, skąd biorą
adres. Reguły, które muszą być wspólne:

* **Plik z internetu zjeżdża do nas** (`upload.downloadUrl`). Adres w sklepie
  przeżyje kilka miesięcy, część leży w warsztacie latami — a pobrane pliki
  obejmuje nocna kopia zapasowa.
* **Miniaturę zajmuje pierwszy, kto ją zastanie** (`becomePreviewIfEmpty`),
  więc własne zdjęcie egzemplarza nie zostaje podmienione na katalogowe.
* **Nazwa musi być unikalna w obrębie części** — Part-DB odpowiada
  `422: name: This value is already used`.
"""

from __future__ import annotations

from typing import Any

from .partdb_client import PartDbError

JsonDict = dict[str, Any]

PICTURE_TYPE_NAMES = {"zdjęcie", "zdjecie", "picture", "image", "photo"}
DATASHEET_TYPE_NAMES = {"datasheet", "dokumentacja", "karta katalogowa"}


async def resolve_type(client: Any, wanted: set[str], create_as: str) -> str:
    """IRI typu załącznika; utwórz, jeśli instancja go nie ma.

    Świeża instalacja Part-DB ma „Zdjęcie" i „Datasheet", ale to dane, nie
    schemat — ktoś mógł je przemianować. Zakładanie brakującego typu jest
    bezpieczne: to etykieta porządkowa, a nie pojemnik w warsztacie.
    """
    for attachment_type in await client.list_attachment_types():
        name = (attachment_type.get("name") or "").strip().casefold()
        if name in wanted:
            return str(attachment_type["@id"])
    created = await client.create("attachment_types", {"name": create_as})
    return str(created["@id"])


async def attach_url(
    client: Any,
    part_iri: str,
    *,
    name: str,
    url: str,
    attachment_type: str,
    preview: bool = True,
    warnings: list[str] | None = None,
) -> str:
    """Dopnij plik spod adresu i każ Part-DB pobrać go do siebie."""
    payload: JsonDict = {
        "name": name,
        "attachment_type": attachment_type,
        "element": part_iri,
        "url": url,
        "upload": {"downloadUrl": True, "becomePreviewIfEmpty": preview},
    }
    try:
        attachment = await client.create_attachment(payload)
    except PartDbError as exc:
        # Pobieranie plików jest w Part-DB **domyślnie wyłączone**
        # (`ALLOW_ATTACHMENT_DOWNLOADS`); wtedy instancja odpowiada 500
        # z komunikatem „Download of attachments is not allowed!". Sam
        # odnośnik jest nadal lepszy niż nic — ale użytkownik musi wiedzieć,
        # że plik został u dostawcy i zniknie razem z jego adresem.
        if "download" not in str(exc).lower() and "500" not in str(exc):
            raise
        payload.pop("upload")
        attachment = await client.create_attachment(payload)
        if warnings is not None:
            warnings.append(
                f"{name}: instancja Part-DB nie pozwala pobierać plików "
                "(ALLOW_ATTACHMENT_DOWNLOADS), więc został sam odnośnik — "
                "zniknie, gdy sklep przebuduje adresy."
            )
    return str(attachment["@id"])
