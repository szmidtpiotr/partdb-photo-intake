"""Przypisanie pojemnika rzeczom zarejestrowanym luzem (#62, ADR-017).

ADR-017 rozdzielił dwa kroki, które wcześniej były sklejone: **rejestrację**
(część trafia do magazynu ze stanem, bez pojemnika) i **przypisanie**
(dostaje pudło, gdy struktura już się wyłoniła). Rejestracja działa od
intake'u; to jest ta druga połowa.

Przebieg, pod który to jest napisane, wygląda przy stole tak: *biorę rzecz
z kartonu → pytam bazę, gdzie ma trafić → wkładam do kuwety*. Dlatego
narzędzie przyjmuje **listę rzeczy albo całą kategorię** — przy sortowaniu
najczęstszy ruch to „wszystkie kondensatory SMD do tej szuflady".

Dwie granice, obie celowe:

1. **Ruszamy wyłącznie stany bez pojemnika.** Przeniesienie czegoś, co już
   gdzieś leży, to inna operacja i inne ryzyko (#38) — tutaj kończy się
   pominięciem i adnotacją w planie, nie cichą zmianą.
2. **Pojemnika nie tworzymy** (ADR-017). Nieznana nazwa to błąd, a nie
   powód do założenia pudła, którego nie ma na półce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .locations import LocationError, location_label, resolve_location
from .quantity import UNCOUNTED
from .selection import SelectionError, name_of, select_parts

JsonDict = dict[str, Any]


class AssignError(RuntimeError):
    """Przypisanie jest niewykonalne — nie ma takiego pojemnika, nie ma czego
    przypisać, albo nie wiadomo, o którą część chodzi."""


def _placed(lot: JsonDict) -> str:
    location = lot.get("storage_location") or {}
    return str(location.get("full_path") or location.get("name") or "")


def _amount_text(lot: JsonDict) -> str:
    """Ilość tak, jak pokazuje ją zrzut stanu — `~` znaczy kubełek (ADR-007)."""
    amount = lot.get("amount") or 0
    if lot.get("instock_unknown"):
        return UNCOUNTED if not amount else f"~{amount:g}"
    return f"x{amount:g}"


@dataclass(frozen=True)
class PlannedLot:
    part_name: str
    lot_iri: str
    amount: str
    description: str = ""

    def describe(self) -> str:
        bits = [self.part_name, self.amount]
        if self.description:
            bits.append(self.description)
        return " · ".join(bit for bit in bits if bit)


@dataclass
class AssignPlan:
    location: JsonDict
    lots: list[PlannedLot] = field(default_factory=list)
    # Rzeczy pominięte i **powód** pominięcia. Cicha odmowa byłaby tu
    # najgorsza: użytkownik odszedłby od stołu przekonany, że pudło jest
    # opisane w bazie, a połowa zawartości nadal wisiałaby luzem.
    already_placed: list[tuple[str, str]] = field(default_factory=list)
    without_stock: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.ambiguous)

    def skipped_text(self) -> str:
        """Co zostało pominięte i dlaczego — osobno, bo ta lista jest równie
        ważna po zapisie jak przed nim. Bez niej „przypisane" czytałoby się
        jak „wszystko przypisane"."""
        lines: list[str] = []
        if self.already_placed:
            lines += ["POMIJAM, bo już mają pojemnik:"]
            lines += [f"  - {name} → {where}" for name, where in self.already_placed]
            lines.append(
                "  (przeprowadzka rzeczy już położonej to osobna operacja — #38)"
            )
        if self.without_stock:
            if lines:
                lines.append("")
            lines += ["POMIJAM, bo to wpisy katalogowe bez stanu:"]
            lines += [f"  - {name}" for name in self.without_stock]
            lines.append("  (znam tę część, ale jej nie mam — nie ma czego włożyć)")
        if self.unknown:
            if lines:
                lines.append("")
            lines += ["NIE ZNALAZŁEM w magazynie:"]
            lines += [f"  - {name}" for name in self.unknown]
        return "\n".join(lines)

    def as_text(self) -> str:
        lines = [f"Pojemnik: {location_label(self.location)}", ""]

        if self.lots:
            lines.append(f"DO PRZYPISANIA ({len(self.lots)}):")
            lines += [f"  {i + 1}. {lot.describe()}" for i, lot in enumerate(self.lots)]
        else:
            lines.append("Nie ma czego przypisać — żaden stan nie czeka luzem.")

        skipped = self.skipped_text()
        if skipped:
            lines += ["", skipped]

        if self.ambiguous:
            lines += ["", "WSTRZYMANE — nie wiadomo, o którą część chodzi:"]
            for wanted, candidates in self.ambiguous:
                lines.append(f"  - {wanted} → {', '.join(candidates)}")
            lines.append(
                "  Podaj dokładną nazwę albo identyfikator części z Part-DB "
                "(`parts/29` albo samo `29`). "
                "Zapis z confirm=true odmówi, dopóki to wisi — "
                "wsadzenie nie tej części do pudła jest gorsze niż jej brak."
            )
        elif self.lots:
            lines += [
                "",
                "Nic jeszcze nie zapisano. Zatwierdź przez confirm=true.",
                "Po przypisaniu wydrukuj etykietę pojemnika z UI Part-DB (#34) — "
                "pudło bez etykiety znajduje się tylko przez bazę.",
            ]
        return "\n".join(lines)


class LocationAssigner:
    """Buduje plan przypisania i — po potwierdzeniu — wykonuje go."""

    def __init__(self, client: Any):
        self.client = client

    async def plan(
        self,
        location: str,
        *,
        parts: list[str] | None = None,
        category: str = "",
    ) -> AssignPlan:
        wanted = [p for p in (parts or []) if p and p.strip()]
        category = (category or "").strip()
        if not wanted and not category:
            raise AssignError(
                "Podaj, co przypisać: listę części (`parts`) albo kategorię "
                "(`category`). Przypisanie „wszystkiego” byłoby zgadywaniem."
            )
        if wanted and category:
            raise AssignError(
                "Podaj albo listę części, albo kategorię — nie oba naraz. "
                "Inaczej nie wiadomo, co ma pierwszeństwo przy rozbieżności."
            )

        try:
            resolved = await resolve_location(self.client, location)
        except LocationError as exc:
            raise AssignError(str(exc)) from exc
        if resolved is None:
            raise AssignError(
                "Pusta lokalizacja. To narzędzie służy do wskazania pojemnika; "
                "stan bez przypisania powstaje sam przy intake'u (ADR-017)."
            )

        all_parts = await self.client.list_parts()
        try:
            selection = select_parts(all_parts, wanted, category)
        except SelectionError as exc:
            raise AssignError(str(exc)) from exc

        plan = AssignPlan(
            location=resolved,
            unknown=selection.unknown,
            ambiguous=selection.ambiguous,
        )
        for part in selection.chosen:
            lots = part.get("partLots") or []
            if not lots:
                plan.without_stock.append(name_of(part))
                continue
            for lot in lots:
                where = _placed(lot)
                if where:
                    plan.already_placed.append((name_of(part), where))
                    continue
                iri = lot.get("@id")
                if not iri:
                    # Bez identyfikatora nie ma czego zmienić. Zgadywanie
                    # adresu byłoby obchodzeniem klienta REST (reguła 4).
                    plan.unknown.append(
                        f"{name_of(part)}: stan bez identyfikatora w Part-DB"
                    )
                    continue
                plan.lots.append(
                    PlannedLot(
                        part_name=name_of(part),
                        lot_iri=str(iri),
                        amount=_amount_text(lot),
                        description=(lot.get("description") or "").strip(),
                    )
                )
        return plan

    async def execute(self, plan: AssignPlan) -> list[str]:
        """Wykonaj plan. Zwróć IRI zmienionych stanów."""
        if plan.blocked:
            raise AssignError(
                "Nierozstrzygnięte pozycje: "
                + "; ".join(name for name, _ in plan.ambiguous)
                + ". Podaj dokładną nazwę albo IRI zamiast zgadywać."
            )
        if not plan.lots:
            raise AssignError(
                "Nie ma czego przypisać — wszystkie wskazane rzeczy albo już "
                "mają pojemnik, albo nie mają stanu."
            )

        location_iri = plan.location["@id"]
        changed: list[str] = []
        for lot in plan.lots:
            await self.client.patch(lot.lot_iri, {"storage_location": location_iri})
            changed.append(lot.lot_iri)
        return changed
