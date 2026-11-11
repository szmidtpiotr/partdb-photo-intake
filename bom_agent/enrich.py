"""Zdjęcia katalogowe, datasheety i parametry od dostawcy (#67).

Magazyn spisany ze zdjęć zna nazwę części i to, że leży w kartonie. Nie zna
jej dokumentacji — a przy projektowaniu układu to właśnie datasheet i
parametry rozstrzygają, czy stabilizator wyrobi 1 A i jaki ma pinout.

Dane bierzemy z info providerów Part-DB (TME, LCSC), przez REST. Zmierzone
na instancji, bo dostawcy różnią się tym, co w ogóle oddają:

| | TME | LCSC |
|---|---|---|
| miniatura w wynikach | tak | tak |
| pełne zdjęcia w szczegółach | **nie** | tak, 900×900 |
| parametry | dużo (15 na moduł) | mniej (5) |

Trzy zasady, wszystkie z tego samego korzenia — fałszywe dane są gorsze niż
ich brak:

1. **Trafienie po numerze katalogowym bierzemy, po nazwie pytamy.** Zapytanie
   TME o `NE555` zwróciło jako pierwszy wynik płytkę deweloperską MIKROE-2032.
   Automat dokleiłby do układu scalonego zdjęcie czegoś innego.
2. **Plik zjeżdża do Part-DB, nie zostaje linkiem.** `upload.downloadUrl`
   każe Part-DB pobrać zdjęcie do siebie — inaczej pierwsza przebudowa CDN-u
   dostawcy zostawia w bazie puste ramki, a kopia zapasowa nie ma czego objąć.
3. **Własne zdjęcie zostaje miniaturą.** Katalogowe pokazuje wzorzec, Twoje —
   tę sztukę z wytartym sitodrukiem. `becomePreviewIfEmpty` ustawia miniaturę
   tylko wtedy, gdy części jeszcze żadnej nie ma.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .attachments import (
    DATASHEET_TYPE_NAMES,
    PICTURE_TYPE_NAMES,
    attach_url,
    resolve_type,
)
from .naming import normalize
from .partdb_client import PartDbError
from .selection import SelectionError, mpn_of, name_of, select_parts

JsonDict = dict[str, Any]

# Ile części obsłużyć w jednym wywołaniu. Każda to zapytanie do dostawcy,
# a TME i LCSC mają limity — cała kategoria kondensatorów (80 pozycji)
# byłaby prośbą o blokadę. Ucięcie jest jawne w planie, nie ciche.
DEFAULT_LIMIT = 10


class EnrichError(RuntimeError):
    """Uzupełnienie danych jest niewykonalne."""


# Znaki sterujące query stringu dostawcy. Fraza idzie do TME dosłownie
# w URL-u (`?phrase=…`); `+` w treści kończy się odpowiedzią 400, której
# Part-DB nie łapie i oddaje 500, wywracając całą partię (#71).
_QUERY_NOISE = re.compile(r"[+&?#%/\\]+")


def search_phrase(name: str) -> str:
    """Nazwa handlowa do wyszukiwarki dostawcy — nie nasz opis.

    Część spisana ze zdjęcia nazywa się po polsku i bywa opatrzona dopiskiem
    w nawiasie: „WeMos D1 mini Battery Shield (ładowarka Li-Po + step-up 5V)".
    Ten dopisek nie identyfikuje części u dostawcy, a znak `+` w nim wywraca
    zapytanie do TME (#71). Obcinamy więc zawartość nawiasów i znaki sterujące
    zapytania, zostawiając sam człon handlowy — „WeMos D1 mini Battery Shield".

    Fraza pada dopiero, gdy nie ma ani numeru katalogowego, ani oznaczenia
    wyłuskanego z nazwy; wersje typu `(N16R8)` łapie wcześniej `designation_of`,
    więc kasowanie nawiasów tu niczego istotnego nie gubi.
    """
    without_parens = re.sub(r"[\[(][^\])]*[\])]", " ", name or "")
    cleaned = _QUERY_NOISE.sub(" ", without_parens)
    return " ".join(cleaned.split())


def designation_of(name: str) -> str:
    """Wyłuskaj oznaczenie z ludzkiej nazwy: `HC-SR501 czujnik ruchu PIR`
    → `HC-SR501`.

    Części spisane ze zdjęć nazywają się po polsku i nie mają numeru
    katalogowego. Bez tego kroku zapytanie do dostawcy brzmiałoby „HC-SR501
    czujnik ruchu PIR" i nie trafiało w nic. Oznaczeniem jest pierwszy człon
    mający **literę i cyfrę** — ta sama reguła co przy deduplikacji, i z tego
    samego powodu: `1206` czy `100` same w sobie nie identyfikują części.
    """
    for word in (name or "").replace("/", " ").split():
        candidate = word.strip(",.;:()[]")
        if (
            len(candidate) >= 4
            and any(char.isdigit() for char in candidate)
            and any(char.isalpha() for char in candidate)
        ):
            return candidate
    return ""


@dataclass(frozen=True)
class ProviderHit:
    """Część u dostawcy, uznana za tę samą co nasza."""

    provider_key: str
    provider_id: str
    name: str
    mpn: str
    matched_by: str  # "MPN", "oznaczenie …", "nazwa" albo "wskazane ręcznie"
    url: str = ""
    # Miniatura przychodzi z WYNIKU WYSZUKIWANIA, nie ze szczegółów — i tylko
    # tyle daje TME. Bez przeniesienia jej tutaj część z TME zostawałaby bez
    # żadnego zdjęcia, mimo że dostawca jedno ma.
    preview_url: str = ""

    def label(self) -> str:
        return (
            f"{self.provider_key.upper()} {self.provider_id} — {self.name} "
            f"(zgodność po: {self.matched_by})"
        )


@dataclass
class PlannedEnrichment:
    part: JsonDict
    hit: ProviderHit
    picture_url: str = ""
    datasheet_url: str = ""
    parameters: list[JsonDict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def part_name(self) -> str:
        return name_of(self.part)

    @property
    def empty(self) -> bool:
        return not (self.picture_url or self.datasheet_url or self.parameters)

    def describe(self) -> str:
        adds = []
        if self.picture_url:
            adds.append("zdjęcie katalogowe")
        if self.datasheet_url:
            adds.append("datasheet")
        if self.parameters:
            adds.append(f"{len(self.parameters)} parametrów")
        what = ", ".join(adds) if adds else "nic nowego"
        line = f"{self.part_name} → {what}\n       źródło: {self.hit.label()}"
        if self.skipped:
            line += f"\n       już ma: {', '.join(self.skipped)}"
        return line


@dataclass
class EnrichPlan:
    items: list[PlannedEnrichment] = field(default_factory=list)
    # Powody, dla których część została pominięta. Bez nich „uzupełnione"
    # czytałoby się jak „wszystko uzupełnione".
    no_provider_match: list[tuple[str, list[str]]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)
    truncated: int = 0
    # Rzeczy, które nie wyszły, ale nie unieważniają zapisu — np. instancja
    # bez zgody na pobieranie plików. Dane są ważniejsze niż to, gdzie leży
    # plik, więc takie potknięcie ma zostać zgłoszone, a nie wywrócić zapis.
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.ambiguous)

    @property
    def useful(self) -> list[PlannedEnrichment]:
        return [item for item in self.items if not item.empty]

    def as_text(self) -> str:
        lines: list[str] = []
        if self.useful:
            lines.append(f"DO UZUPEŁNIENIA ({len(self.useful)}):")
            lines += [
                f"  {i + 1}. {item.describe()}" for i, item in enumerate(self.useful)
            ]
        elif self.items:
            lines.append(
                "Nie ma co dołożyć — te części mają już wszystko, co dostawca oddaje."
            )
        else:
            lines.append("Nic do dołożenia — powody niżej.")

        nothing_new = [item for item in self.items if item.empty]
        if nothing_new:
            lines += ["", "BEZ ZMIAN (dostawca nie ma nic ponad to, co jest):"]
            lines += [f"  - {item.part_name}" for item in nothing_new]

        if self.no_provider_match:
            lines += ["", "NIE ZNALAZŁEM U DOSTAWCY:"]
            for name, candidates in self.no_provider_match:
                if candidates:
                    lines.append(f"  - {name} → było podobne: {', '.join(candidates)}")
                    lines.append(
                        "    (podaj numer katalogowy, jeśli to ta sama rzecz — "
                        "po nazwie nie zgaduję)"
                    )
                else:
                    lines.append(f"  - {name}")

        if self.unknown:
            lines += ["", "NIE MA W MAGAZYNIE:"] + [f"  - {n}" for n in self.unknown]

        if self.ambiguous:
            lines += ["", "WSTRZYMANE — nie wiadomo, o którą część chodzi:"]
            for wanted, candidates in self.ambiguous:
                lines.append(f"  - {wanted} → {', '.join(candidates)}")

        if self.truncated:
            lines += [
                "",
                f"UCIĘTE: pominąłem {self.truncated} części z powodu limitu "
                "na jedno wywołanie (dostawcy mają limity zapytań). "
                "Zawołaj ponownie, żeby wziąć resztę.",
            ]

        if self.warnings:
            lines += ["", "UWAGI:"] + [f"  ! {w}" for w in self.warnings]

        if self.useful and not self.blocked:
            lines += [
                "",
                "Nic jeszcze nie zapisano. Zatwierdź przez confirm=true.",
                "Pliki zjadą do Part-DB (nie zostaną linkiem do sklepu), "
                "a miniaturą części zostanie Twoje własne zdjęcie, jeśli je ma.",
            ]
        return "\n".join(lines)


class PartEnricher:
    """Dobiera dane u dostawcy i — po potwierdzeniu — dopina je do części."""

    def __init__(self, client: Any):
        self.client = client
        self._providers: list[str] | None = None

    async def providers(self) -> list[str]:
        """Klucze aktywnych providerów. Domyślne wyszukiwanie nie jest na tej
        instancji skonfigurowane, więc pytamy wprost o wszystkie aktywne."""
        if self._providers is None:
            self._providers = [
                str(provider["key"])
                for provider in await self.client.list_info_providers()
                if provider.get("key")
            ]
        return self._providers

    async def find_hit(self, part: JsonDict) -> tuple[ProviderHit | None, list[str]]:
        """Znajdź część u dostawcy. Zwróć (trafienie, kandydaci do pokazania).

        Trafieniem jest zgodność numeru katalogowego, zgodność oznaczenia
        wyłuskanego z nazwy albo dokładna zgodność całej nazwy. Reszta wraca
        jako kandydaci — do rozstrzygnięcia przez człowieka, nie przez
        pierwszy wynik z listy.
        """
        providers = await self.providers()
        mpn = mpn_of(part)
        designation = designation_of(name_of(part))
        keyword = mpn or designation or search_phrase(name_of(part))
        if not keyword.strip():
            # Nazwa złożona z samego dopisku w nawiasie — nie ma czym pytać.
            return None, []
        results = await self.client.search_info_providers(keyword, providers)

        candidates: list[str] = []
        for result in results:
            result_mpn = (result.get("mpn") or "").strip()
            result_name = (result.get("name") or "").strip()
            if mpn and result_mpn and normalize(result_mpn) == normalize(mpn):
                return self._hit(result, "MPN"), []
            # Część spisana ze zdjęcia nazywa się po ludzku („HC-SR501 czujnik
            # ruchu PIR"), a numeru katalogowego nie ma wcale. Oznaczenie
            # wyłuskane z nazwy jest wtedy jedynym pewnym punktem zaczepienia —
            # ale musi zgadzać się z numerem katalogowym u dostawcy, nie
            # z jego nazwą handlową.
            if not mpn and designation and result_mpn:
                if normalize(result_mpn) == normalize(designation):
                    return self._hit(result, f"oznaczenie {designation}"), []
            if not mpn and normalize(result_name) == normalize(name_of(part)):
                return self._hit(result, "nazwa"), []
            # W etykiecie musi być numer katalogowy: to po nim widać, że
            # „NE555" u dostawcy jest w rzeczywistości płytką MIKROE-2032.
            label = " ".join(
                bit
                for bit in (
                    str(result.get("provider_key") or "?"),
                    result_mpn,
                    f"({result_name})" if result_name else "",
                )
                if bit
            )
            if label.strip() and len(candidates) < 5:
                candidates.append(label)
        return None, candidates

    @staticmethod
    def _hit(result: JsonDict, matched_by: str) -> ProviderHit:
        return ProviderHit(
            provider_key=str(result.get("provider_key") or "?"),
            provider_id=str(result.get("provider_id") or ""),
            name=str(result.get("name") or ""),
            mpn=str(result.get("mpn") or ""),
            matched_by=matched_by,
            url=str(result.get("provider_url") or ""),
            preview_url=str(result.get("preview_image_url") or ""),
        )

    async def plan(
        self,
        *,
        parts: list[str] | None = None,
        category: str = "",
        limit: int = DEFAULT_LIMIT,
        provider_key: str = "",
        provider_id: str = "",
    ) -> EnrichPlan:
        wanted = [p for p in (parts or []) if p and p.strip()]
        # Wskazanie rekordu u dostawcy wprost: agent pokazał kandydatów,
        # człowiek powiedział „to ten". To jedyna droga, którą da się dołożyć
        # dane części, której nazwa nie zgadza się z katalogiem — i jest
        # świadoma, więc wolno jej ominąć dopasowywanie.
        pick: ProviderHit | None = None
        if provider_key or provider_id:
            if not (provider_key and provider_id):
                raise EnrichError(
                    "Podaj oba: `provider_key` (np. `lcsc`) i `provider_id` "
                    "(np. `C5125085`)."
                )
            pick = ProviderHit(
                provider_key=provider_key.strip(),
                provider_id=provider_id.strip(),
                name="",
                mpn="",
                matched_by="wskazane ręcznie",
            )
        category = (category or "").strip()
        if not wanted and not category:
            raise EnrichError(
                "Podaj, co uzupełnić: listę części (`parts`) albo kategorię "
                "(`category`)."
            )
        if wanted and category:
            raise EnrichError(
                "Podaj albo listę części, albo kategorię — nie oba naraz."
            )

        all_parts = await self.client.list_parts()
        try:
            selection = select_parts(all_parts, wanted, category)
        except SelectionError as exc:
            raise EnrichError(str(exc)) from exc

        plan = EnrichPlan(unknown=selection.unknown, ambiguous=selection.ambiguous)
        if plan.blocked:
            return plan

        chosen = selection.chosen[: max(1, limit)]
        plan.truncated = len(selection.chosen) - len(chosen)

        if pick is not None and len(chosen) != 1:
            raise EnrichError(
                "Wskazanie części u dostawcy działa tylko dla JEDNEJ części — "
                f"wybrano {len(chosen)}. Inaczej nie wiadomo, do której "
                "przypiąć wskazany rekord."
            )

        attachments = await self._attachments_by_part()
        for part in chosen:
            try:
                if pick is not None:
                    hit, candidates = pick, []
                else:
                    hit, candidates = await self.find_hit(part)
                if hit is None:
                    plan.no_provider_match.append((name_of(part), candidates))
                    continue

                details = await self.client.info_provider_details(
                    hit.provider_key, hit.provider_id
                )
            except PartDbError as exc:
                # Awaria zapytania do dostawcy dla JEDNEJ części nie może
                # wywrócić całej partii intake'u (#71). Degradujemy do „nie
                # znalazłem u dostawcy" przy tej pozycji i lecimy dalej —
                # to odczyt, nic nie zapisano.
                plan.no_provider_match.append((name_of(part), []))
                plan.warnings.append(
                    f"{name_of(part)}: dostawca odrzucił zapytanie ({exc}) — "
                    "pominięto tę część, reszta bez zmian."
                )
                continue

            plan.items.append(
                self._plan_one(part, hit, details, attachments.get(part["@id"], []))
            )
        return plan

    async def _attachments_by_part(self) -> dict[str, list[JsonDict]]:
        """Załączniki w jednym zapytaniu, indeksowane po części — inaczej
        sprawdzanie „czy już to ma" byłoby zapytaniem na każdą część."""
        index: dict[str, list[JsonDict]] = {}
        for attachment in await self.client.list_attachments():
            element = attachment.get("element") or {}
            iri = element.get("@id") if isinstance(element, dict) else element
            if iri:
                index.setdefault(str(iri), []).append(attachment)
        return index

    def _plan_one(
        self,
        part: JsonDict,
        hit: ProviderHit,
        details: JsonDict,
        existing: list[JsonDict],
    ) -> PlannedEnrichment:
        planned = PlannedEnrichment(part=part, hit=hit)

        have_pictures = {
            str(a.get("external_path") or "") for a in existing if a.get("picture")
        }
        have_any = {str(a.get("external_path") or "") for a in existing}
        have_names = {str(a.get("name") or "") for a in existing}

        images = [f.get("url") for f in (details.get("images") or []) if f.get("url")]
        # Zdjęcia pełnowymiarowe są tylko u części dostawców (LCSC tak, TME
        # nie); miniatura z wyników wyszukiwania jest lepsza niż nic.
        picture = images[0] if images else hit.preview_url
        if picture and picture not in have_any:
            planned.picture_url = str(picture)
        elif picture:
            planned.skipped.append("zdjęcie katalogowe")
        elif have_pictures:
            planned.skipped.append("zdjęcie")

        sheets = [
            f.get("url") for f in (details.get("datasheets") or []) if f.get("url")
        ]
        if sheets and sheets[0] not in have_any:
            planned.datasheet_url = str(sheets[0])
        elif sheets:
            planned.skipped.append("datasheet")

        existing_parameters = {
            normalize(p.get("name") or "") for p in (part.get("parameters") or [])
        }
        for raw in details.get("parameters") or []:
            name = (raw.get("name") or "").strip()
            if not name or normalize(name) in existing_parameters:
                continue
            planned.parameters.append(_parameter_payload(raw))

        # Nazwy załączników muszą być unikalne w obrębie części — Part-DB
        # odpowiada `422: name: This value is already used`.
        if planned.picture_url and _picture_name(hit) in have_names:
            planned.picture_url = ""
            planned.skipped.append("zdjęcie katalogowe (taka nazwa już jest)")
        if planned.datasheet_url and _datasheet_name(hit) in have_names:
            planned.datasheet_url = ""
            planned.skipped.append("datasheet (taka nazwa już jest)")
        return planned

    async def execute(self, plan: EnrichPlan) -> list[str]:
        if plan.blocked:
            raise EnrichError(
                "Nierozstrzygnięte pozycje: "
                + "; ".join(name for name, _ in plan.ambiguous)
            )
        if not plan.useful:
            raise EnrichError("Nie ma czego dołożyć.")

        picture_type: str | None = None
        datasheet_type: str | None = None
        created: list[str] = []

        for item in plan.useful:
            if item.picture_url:
                if picture_type is None:
                    picture_type = await resolve_type(
                        self.client, PICTURE_TYPE_NAMES, "Zdjęcie"
                    )
                created.append(
                    await attach_url(
                        self.client,
                        item.part["@id"],
                        name=_picture_name(item.hit),
                        url=item.picture_url,
                        attachment_type=picture_type,
                        warnings=plan.warnings,
                    )
                )
            if item.datasheet_url:
                if datasheet_type is None:
                    datasheet_type = await resolve_type(
                        self.client, DATASHEET_TYPE_NAMES, "Datasheet"
                    )
                created.append(
                    await attach_url(
                        self.client,
                        item.part["@id"],
                        name=_datasheet_name(item.hit),
                        url=item.datasheet_url,
                        attachment_type=datasheet_type,
                        preview=False,
                        warnings=plan.warnings,
                    )
                )
            for parameter in item.parameters:
                result = await self.client.create_parameter(
                    {**parameter, "element": item.part["@id"]}
                )
                created.append(str(result["@id"]))
        return created


def _picture_name(hit: ProviderHit) -> str:
    return f"Zdjęcie katalogowe ({hit.provider_key.upper()} {hit.provider_id})"


def _datasheet_name(hit: ProviderHit) -> str:
    return f"Datasheet ({hit.provider_key.upper()} {hit.provider_id})"


def _parameter_payload(raw: JsonDict) -> JsonDict:
    """`ParameterDTO` dostawcy → `Parameter-Write` Part-DB.

    Uwaga na `value_typ` → `value_typical`: nazwy różnią się między DTO
    providera a encją, a cicha utrata wartości typowej zostawiłaby parametr
    bez liczby.
    """
    payload: JsonDict = {"name": (raw.get("name") or "").strip()}
    for source, target in (
        ("value_text", "value_text"),
        ("value_typ", "value_typical"),
        ("value_min", "value_min"),
        ("value_max", "value_max"),
        ("unit", "unit"),
        ("symbol", "symbol"),
        ("group", "group"),
    ):
        value = raw.get(source)
        if value not in (None, ""):
            payload[target] = value
    return payload
