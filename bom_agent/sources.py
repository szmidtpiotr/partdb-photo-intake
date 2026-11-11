"""Skąd to jest i po ile — moduły ze sklepów spoza katalogów (#68).

TME i LCSC mają API i wbudowanego providera (#67). Botland i Allegro nie
mają ani jednego, ani drugiego, a stamtąd bierze się większość modułów
w tym warsztacie. Zmierzone: strona Botlandu daje się odczytać i niesie
nazwę, indeks, cenę, zdjęcie i tabelę parametrów; Allegro odpowiada
**403** na każdą próbę automatycznego odczytu.

Dlatego dane zbiera **agent w sesji** — czyta stronę sklepu i podaje je
tutaj gotowe (ADR-005: rozpoznawanie i wnioskowanie jest po stronie
agenta, nie osobnej usługi). Ten moduł tylko zapisuje, zawsze po planie.

Trzy rozstrzygnięcia:

1. **Indeks sklepu to nie numer katalogowy producenta.** `KAB-08134` jest
   indeksem Botlandu i w polu MPN blokował wyszukiwanie u dystrybutorów —
   pytaliśmy katalog TME o symbol sklepu. Właściwe miejsce to
   `Orderdetail.supplierpartnr`, i tam go przenosimy.
2. **Zdjęcie ze sklepu zjeżdża do nas.** Oferta na Allegro znika po
   miesiącu, karta produktu w Botlandzie zmienia adres przy przebudowie.
3. **Cena jest informacją historyczną, nie prawdą o dziś.** Zapisujemy ją
   razem z datą zakupu i linkiem, żeby przy „kończy się" było wiadomo,
   gdzie dokupić i czego się spodziewać.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .attachments import (
    DATASHEET_TYPE_NAMES,
    PICTURE_TYPE_NAMES,
    attach_url,
    resolve_type,
)
from .naming import normalize
from .selection import SelectionError, name_of, select_parts

JsonDict = dict[str, Any]


class SourceError(RuntimeError):
    """Zapisu źródła nie da się wykonać."""


@dataclass
class SourcePlan:
    part: JsonDict
    supplier_name: str
    supplier: JsonDict | None = None  # istniejący dostawca albo None (utworzymy)
    supplier_part_number: str = ""
    product_url: str = ""
    price: float | None = None
    picture_url: str = ""
    datasheet_url: str = ""
    description: str = ""
    parameters: list[JsonDict] = field(default_factory=list)
    # Indeks sklepu wpisany w pole producenta — do przeniesienia tam, gdzie
    # jego miejsce. To nie kosmetyka: w polu MPN psuje wyszukiwanie u TME
    # i LCSC, bo tamte katalogi takiego numeru nie znają.
    move_mpn: str = ""
    existing_order: bool = False
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def new_order(self) -> bool:
        """Czy powstanie wpis zakupowy. Sam indeks też jest informacją —
        mówi, pod czym szukać w sklepie, nawet bez linku i ceny."""
        return not self.existing_order and bool(
            self.supplier_part_number or self.product_url or self.price is not None
        )

    @property
    def empty(self) -> bool:
        return not (
            self.picture_url
            or self.datasheet_url
            or self.parameters
            or self.description
            or self.move_mpn
            or self.new_order
        )

    def as_text(self) -> str:
        lines = [f"Część: {name_of(self.part)}", f"Sklep: {self.supplier_name}"]
        if self.supplier is None:
            lines.append("  (dostawca zostanie utworzony — nie ma go jeszcze w bazie)")

        if self.existing_order:
            lines.append("  wpis zakupowy już istnieje — nie dubluję")
        else:
            bits = []
            if self.supplier_part_number:
                bits.append(f"indeks {self.supplier_part_number}")
            if self.product_url:
                bits.append(self.product_url)
            if self.price is not None:
                bits.append(f"{self.price:.2f} zł")
            if bits:
                lines.append("  wpis zakupowy: " + " · ".join(bits))

        adds = []
        if self.picture_url:
            adds.append("zdjęcie ze sklepu")
        if self.datasheet_url:
            adds.append("dokumentacja")
        if self.parameters:
            adds.append(f"{len(self.parameters)} parametrów")
        if self.description:
            adds.append("opis")
        if adds:
            lines.append("  dołożę: " + ", ".join(adds))

        if self.move_mpn:
            lines.append(
                f"  przeniosę {self.move_mpn} z pola producenta do wpisu "
                "zakupowego — to indeks sklepu, a w polu MPN blokuje "
                "wyszukiwanie u TME i LCSC"
            )

        if self.skipped:
            lines.append("  pomijam (już jest): " + ", ".join(self.skipped))
        if self.warnings:
            lines += ["", "UWAGI:"] + [f"  ! {w}" for w in self.warnings]

        if self.empty:
            lines += ["", "Nie ma czego dopisać — ta część ma już wszystko."]
        else:
            lines += ["", "Nic jeszcze nie zapisano. Zatwierdź przez confirm=true."]
        return "\n".join(lines)


class SourceRecorder:
    """Zapisuje, skąd część pochodzi: sklep, indeks, link, cena, zdjęcie."""

    def __init__(self, client: Any):
        self.client = client

    async def plan(
        self,
        part: str,
        supplier: str,
        *,
        supplier_part_number: str = "",
        product_url: str = "",
        price: float | None = None,
        picture_url: str = "",
        datasheet_url: str = "",
        description: str = "",
        parameters: list[JsonDict] | None = None,
    ) -> SourcePlan:
        supplier = (supplier or "").strip()
        if not supplier:
            raise SourceError("Podaj sklep — np. `Botland` albo `Allegro`.")

        parts = await self.client.list_parts()
        try:
            selection = select_parts(parts, [part])
        except SelectionError as exc:
            raise SourceError(str(exc)) from exc
        if selection.ambiguous:
            wanted, candidates = selection.ambiguous[0]
            raise SourceError(
                f"Nie wiadomo, o którą część chodzi przy {wanted!r}: "
                f"{', '.join(candidates)}. Podaj dokładną nazwę albo "
                "identyfikator z Part-DB."
            )
        if not selection.chosen:
            raise SourceError(f"Nie ma w magazynie części {part!r}.")

        chosen = selection.chosen[0]
        plan = SourcePlan(
            part=chosen,
            supplier_name=supplier,
            supplier_part_number=supplier_part_number.strip(),
            product_url=product_url.strip(),
            price=price,
            description=description.strip(),
        )

        for existing in await self.client.list_suppliers():
            if normalize(existing.get("name") or "") == normalize(supplier):
                plan.supplier = existing
                break

        # Wpis zakupowy dublowany po tym samym sklepie i indeksie nic nie
        # wnosi, a rozmnaża się przy każdym powtórzonym wywołaniu.
        for order in await self.client.list_orderdetails():
            order_part = order.get("part")
            iri = order_part.get("@id") if isinstance(order_part, dict) else order_part
            if str(iri) != chosen["@id"]:
                continue
            same_index = normalize(order.get("supplierpartnr") or "") == normalize(
                plan.supplier_part_number
            )
            if same_index:
                plan.existing_order = True
                break

        existing_attachments = [
            attachment
            for attachment in await self.client.list_attachments()
            if _element_iri(attachment) == chosen["@id"]
        ]
        have = {str(a.get("external_path") or "") for a in existing_attachments}
        have_names = {str(a.get("name") or "") for a in existing_attachments}

        if picture_url and picture_url not in have:
            if _picture_name(supplier) in have_names:
                plan.skipped.append("zdjęcie ze sklepu (taka nazwa już jest)")
            else:
                plan.picture_url = picture_url.strip()
        elif picture_url:
            plan.skipped.append("zdjęcie ze sklepu")

        if datasheet_url and datasheet_url not in have:
            if _datasheet_name(supplier) in have_names:
                plan.skipped.append("dokumentacja (taka nazwa już jest)")
            else:
                plan.datasheet_url = datasheet_url.strip()
        elif datasheet_url:
            plan.skipped.append("dokumentacja")

        existing_parameters = {
            normalize(p.get("name") or "") for p in (chosen.get("parameters") or [])
        }
        for raw in parameters or []:
            name = str(raw.get("name") or "").strip()
            value = str(raw.get("value") or raw.get("value_text") or "").strip()
            if not name or not value or normalize(name) in existing_parameters:
                continue
            payload: JsonDict = {"name": name, "value_text": value}
            if raw.get("unit"):
                payload["unit"] = str(raw["unit"])
            plan.parameters.append(payload)

        if plan.description and (chosen.get("description") or "").strip():
            plan.skipped.append("opis")
            plan.description = ""

        mpn = (chosen.get("manufacturer_product_number") or "").strip()
        if mpn and plan.supplier_part_number:
            if normalize(mpn) == normalize(plan.supplier_part_number):
                plan.move_mpn = mpn

        return plan

    async def execute(self, plan: SourcePlan) -> list[str]:
        if plan.empty:
            raise SourceError("Nie ma czego dopisać.")

        created: list[str] = []
        part_iri = plan.part["@id"]

        supplier = plan.supplier
        if supplier is None:
            supplier = await self.client.create(
                "suppliers", {"name": plan.supplier_name}
            )
            created.append(str(supplier["@id"]))

        if plan.new_order:
            order_payload: JsonDict = {
                "part": part_iri,
                "supplier": supplier["@id"],
                "supplierpartnr": plan.supplier_part_number,
            }
            if plan.product_url:
                order_payload["supplier_product_url"] = plan.product_url
            order = await self.client.create("orderdetails", order_payload)
            created.append(str(order["@id"]))

            if plan.price is not None:
                # Waluty nie podajemy: instancja nie ma żadnej zdefiniowanej,
                # a puste pole znaczy „waluta bazowa" — tu PLN.
                price = await self.client.create(
                    "pricedetails",
                    {
                        "orderdetail": order["@id"],
                        "price": f"{plan.price:.2f}",
                        "price_related_quantity": 1,
                        "min_discount_quantity": 1,
                    },
                )
                created.append(str(price["@id"]))

        if plan.picture_url:
            created.append(
                await attach_url(
                    self.client,
                    part_iri,
                    name=_picture_name(plan.supplier_name),
                    url=plan.picture_url,
                    attachment_type=await resolve_type(
                        self.client, PICTURE_TYPE_NAMES, "Zdjęcie"
                    ),
                    warnings=plan.warnings,
                )
            )
        if plan.datasheet_url:
            created.append(
                await attach_url(
                    self.client,
                    part_iri,
                    name=_datasheet_name(plan.supplier_name),
                    url=plan.datasheet_url,
                    attachment_type=await resolve_type(
                        self.client, DATASHEET_TYPE_NAMES, "Datasheet"
                    ),
                    preview=False,
                    warnings=plan.warnings,
                )
            )

        for parameter in plan.parameters:
            result = await self.client.create_parameter(
                {**parameter, "element": part_iri}
            )
            created.append(str(result["@id"]))

        patch: JsonDict = {}
        if plan.description:
            patch["description"] = plan.description
        if plan.move_mpn:
            patch["manufacturer_product_number"] = ""
        if patch:
            await self.client.patch(part_iri, patch)

        return created


def _element_iri(attachment: JsonDict) -> str:
    element = attachment.get("element") or {}
    return str(element.get("@id") if isinstance(element, dict) else element)


def _picture_name(supplier: str) -> str:
    return f"Zdjęcie ze sklepu ({supplier})"


def _datasheet_name(supplier: str) -> str:
    return f"Dokumentacja ({supplier})"
