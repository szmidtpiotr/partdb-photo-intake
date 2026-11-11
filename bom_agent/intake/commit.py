"""Zatwierdzenie rozpoznania: pozycja kolejki → części i stany w Part-DB.

Zasada z 05-mcp-tools: **operacje masowe potwierdzane.** `plan()` mówi, co
się stanie, i niczego nie zapisuje; `execute()` zapisuje dopiero na wyraźne
`confirm`. Pomyłka przy intake'u psuje kilkanaście pozycji naraz, a bez planu
zobaczyłbyś to dopiero w bazie.

Cała komunikacja z Part-DB idzie przez `partdb_client.py` (reguła 4
z 02-architektura) — ten moduł składa tylko ładunki i decyzje.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Reguły porównywania nazw są wspólne dla całego systemu i mieszkają
# w `bom_agent/naming.py`; intake był tylko ich pierwszym użytkownikiem.
from ..locations import LocationError, resolve_location
from ..naming import normalize, tokens
from ..partdb_client import PartDbClient, PartDbError
from ..quantity import UnknownBucket, bucket_of, parse_bucket
from .images import CropBox, CropError

JsonDict = dict[str, Any]

_NUMBER = re.compile(r"\s*-?\d+(?:[.,]\d+)?\s*")


class CommitError(RuntimeError):
    """Zatwierdzenie jest niewykonalne — np. nie ma takiej lokalizacji."""


def _amount_of(raw: JsonDict) -> tuple[float, bool]:
    """Ilość z pozycji: liczba albo kubełek klasy B (#29, ADR-007).

    Dyktowanie mówi „dużo setek nanofaradów", nie „dwieście sztuk", więc
    `amount` przyjmuje też słowo. Przelicznik siedzi w `quantity.py`, żeby
    „dużo" znaczyło to samo w każdej sesji — inaczej zrzut stanu przestaje
    być porównywalny między pozycjami.

    Kubełek sam ustawia `instock_unknown`, bo liczba jest wtedy środkiem
    przedziału, a nie pomiarem.
    """
    bucket = raw.get("bucket")
    amount = raw.get("amount", 1)
    word = str(bucket) if bucket else ""
    if not word and isinstance(amount, str) and not _NUMBER.fullmatch(amount):
        word = amount

    if word:
        try:
            return parse_bucket(word)
        except UnknownBucket as exc:
            raise CommitError(str(exc)) from exc

    if isinstance(amount, str):
        amount = amount.strip().replace(",", ".")
    try:
        return float(amount or 0), False
    except (TypeError, ValueError) as exc:
        raise CommitError(
            f"Ilość {amount!r} nie jest ani liczbą, ani kubełkiem"
        ) from exc


@dataclass(frozen=True)
class SourcePhoto:
    """Zdjęcie z kolejki, na podstawie którego powstało rozpoznanie (#64).

    Przy module bez czytelnego nadruku to jedyny wiarygodny obraz tej
    konkretnej sztuki — katalogowe zdjęcie producenta pokazuje wzorzec,
    a nie egzemplarz z wytartym sitodrukiem, który trzymasz w ręce.

    `crop` zawęża je do **jednej płytki z kadru, na którym leży ich kilka**
    (#72). Wycinek nie jest nowym plikiem: to cztery liczby doklejone do
    adresu, z których serwer odtwarza fragment na żądanie.
    """

    url: str
    item_id: int
    crop: CropBox | None = None

    @property
    def label(self) -> str:
        if self.crop is None:
            return f"Zdjęcie własne (intake #{self.item_id})"
        # Współrzędne w nazwie nie są dla człowieka — są tym, po czym
        # poznasz, że dwa załączniki tej samej pozycji pokazują dwie różne
        # płytki, a nie ten sam kadr dwa razy.
        return (
            f"Zdjęcie własne — wycinek {self.crop.as_param()} (intake #{self.item_id})"
        )

    def cropped(self, crop: CropBox) -> SourcePhoto:
        separator = "&" if "?" in self.url else "?"
        return SourcePhoto(
            url=f"{self.url}{separator}crop={crop.as_param()}",
            item_id=self.item_id,
            crop=crop,
        )


@dataclass
class PartSpec:
    """Jedna rozpoznana pozycja — to, co agent zobaczył na zdjęciu."""

    name: str
    amount: float = 1
    category: str = ""
    mpn: str = ""
    description: str = ""
    manufacturer: str = ""
    instock_unknown: bool = False
    needs_review: bool = False
    note: str = ""
    # Rozstrzygnięcia niejednoznacznej deduplikacji (#24). Puste = agent
    # jeszcze nie zdecydował; wtedy zapis się zatrzymuje i pyta.
    merge_into: str = ""
    force_new: bool = False
    # Wycinek kadru pokazujący **tę jedną** płytkę (#72) i numer ujęcia,
    # z którego go bierzemy (1 = zdjęcie główne pozycji).
    crop: CropBox | None = None
    shot: int = 1

    @classmethod
    def from_dict(cls, raw: JsonDict) -> PartSpec:
        name = (raw.get("name") or "").strip()
        if not name:
            raise CommitError("Pozycja bez nazwy — nie ma czego zapisać")
        amount, uncertain = _amount_of(raw)
        return cls(
            name=name,
            amount=amount,
            category=(raw.get("category") or "").strip(),
            mpn=(raw.get("mpn") or "").strip(),
            description=(raw.get("description") or "").strip(),
            manufacturer=(raw.get("manufacturer") or "").strip(),
            instock_unknown=bool(raw.get("instock_unknown", False)) or uncertain,
            needs_review=bool(raw.get("needs_review", False)),
            note=(raw.get("note") or "").strip(),
            merge_into=(raw.get("merge_into") or "").strip(),
            force_new=bool(raw.get("force_new", False)),
            crop=cls._crop_of(raw),
            shot=cls._shot_of(raw),
        )

    @staticmethod
    def _crop_of(raw: JsonDict) -> CropBox | None:
        crop = raw.get("crop")
        if crop is None or crop == "" or crop == []:
            return None
        try:
            return CropBox.parse(crop)
        except CropError as exc:
            name = (raw.get("name") or "?").strip()
            raise CommitError(f"Wycinek części {name!r}: {exc}") from exc

    @staticmethod
    def _shot_of(raw: JsonDict) -> int:
        shot = raw.get("shot", 1)
        try:
            index = int(shot)
        except (TypeError, ValueError) as exc:
            raise CommitError(f"Numer ujęcia {shot!r} nie jest liczbą") from exc
        if index < 1:
            raise CommitError(f"Ujęcia numerujemy od 1, a nie od {index}")
        return index


@dataclass
class PlannedPart:
    spec: PartSpec
    existing_part: JsonDict | None = None
    new_category: str = ""
    # Części podobne, ale nie takie same. Niepusta lista wstrzymuje zapis:
    # o tym rozstrzyga człowiek, nie heurystyka (06-intake).
    similar: list[JsonDict] = field(default_factory=list)
    # Obrazy dla **tej** części: wszystkie ujęcia pozycji, a przy zdjęciu
    # grupowym — sam wycinek z jej płytką (#72).
    photos: list[SourcePhoto] = field(default_factory=list)

    @property
    def undecided(self) -> bool:
        return bool(self.similar) and self.existing_part is None

    @property
    def action(self) -> str:
        if self.undecided:
            return "DO ROZSTRZYGNIĘCIA"
        return "dołoży stan" if self.existing_part else "utworzy część"

    def describe(self) -> str:
        bits = [f"{self.action}: {self.spec.name}"]
        if self.spec.mpn:
            bits.append(f"MPN {self.spec.mpn}")
        # Przy klasie B liczba jest środkiem przedziału, więc obok niej stoi
        # słowo kubełka — „~200" czyta się jak pomiar, „~200 (dużo)" nie.
        amount = (
            f"~{self.spec.amount:g} ({bucket_of(self.spec.amount)})"
            if self.spec.instock_unknown
            else f"{self.spec.amount:g} szt."
        )
        bits.append(amount)
        if self.existing_part:
            bits.append(f"do istniejącej {self.existing_part['@id']}")
        if self.new_category:
            bits.append(f"NOWA kategoria: {self.new_category}")
        if self.spec.crop:
            bits.append(f"wycinek ujęcia {self.spec.shot}: {self.spec.crop.as_param()}")
        if self.spec.needs_review:
            bits.append("[do sprawdzenia]")
        line = " · ".join(bits)

        if self.undecided:
            candidates = "; ".join(
                f"{part.get('name')} ({part['@id']})" for part in self.similar
            )
            line += (
                f"\n       podobne w bazie: {candidates}"
                '\n       → dopisz merge_into="<IRI>", jeśli to ta sama część, '
                "albo force_new=true, jeśli inna"
            )
        return line


@dataclass
class CommitPlan:
    location: JsonDict | None
    parts: list[PlannedPart] = field(default_factory=list)
    # Wszystkie ujęcia pozycji kolejki (#66). Pierwsze zostaje miniaturą,
    # reszta dokłada widok od spodu i zbliżenie na sitodruk.
    photos: list[SourcePhoto] = field(default_factory=list)
    # Rzeczy, które nie wyszły, ale nie unieważniają zapisu — np. nieudane
    # podpięcie zdjęcia. Stan w magazynie jest ważniejszy niż ilustracja,
    # więc takie potknięcie ma zostać zgłoszone, a nie wywrócić commit.
    warnings: list[str] = field(default_factory=list)

    @property
    def undecided(self) -> list[PlannedPart]:
        return [part for part in self.parts if part.undecided]

    def as_text(self) -> str:
        if self.location:
            location = self.location.get("full_path") or self.location.get("name")
        else:
            location = "BEZ PRZYPISANIA — do rozłożenia przy sortowaniu"
        lines = [f"Lokalizacja: {location}", ""]
        lines += [f"  {i + 1}. {p.describe()}" for i, p in enumerate(self.parts)]
        whole = [
            p
            for p in self.parts
            if p.existing_part is None and p.spec.crop is None and p.photos
        ]
        if self.photos and whole:
            count = len(self.photos)
            what = "zdjęcie z kolejki" if count == 1 else f"{count} ujęć z kolejki"
            lines += [
                "",
                f"Nowe części dostaną {what}: "
                + ", ".join(photo.url for photo in self.photos),
            ]
        # Wycinki wypisujemy osobno i po nazwie części, bo to jedyne miejsce,
        # w którym da się zauważyć przed zapisem, że dwie płytki dostały tę
        # samą ramkę — czyli że jedna z nich ma zdjęcie czegoś innego.
        cut = [p for p in self.parts if p.spec.crop is not None]
        if cut:
            lines += ["", "Wycinki ze zdjęcia grupowego — każda część dostanie swój:"]
            lines += [
                f"  · {p.spec.name} → {photo.url}" for p in cut for photo in p.photos
            ]
            without = [p.spec.name for p in cut if not p.photos]
            if without:
                lines += [f"  ! bez zdjęcia (część już istnieje): {', '.join(without)}"]
        new_categories = {p.new_category for p in self.parts if p.new_category}
        if new_categories:
            lines += [
                "",
                f"Nowe kategorie do utworzenia: {', '.join(sorted(new_categories))}",
            ]
        if self.undecided:
            lines += [
                "",
                f"WSTRZYMANE: {len(self.undecided)} poz. ma podobne części w bazie. "
                "Zapytaj użytkownika, czy to te same, i rozstrzygnij — "
                "zapis z confirm=true odmówi, dopóki tego nie zrobisz.",
            ]
        else:
            lines += ["", "Nic jeszcze nie zapisano. Zatwierdź przez confirm=true."]
        if self.warnings:
            lines += ["", "UWAGI:"] + [f"  ! {warning}" for warning in self.warnings]
        return "\n".join(lines)


class IntakeCommitter:
    """Buduje plan zapisu i — po potwierdzeniu — wykonuje go."""

    def __init__(self, client: PartDbClient):
        self.client = client

    # --- rozwiązywanie odwołań --------------------------------------------

    async def resolve_location(self, wanted: str) -> JsonDict | None:
        """Znajdź lokalizację po nazwie, pełnej ścieżce albo kodzie `A/A1`.

        **Pusta wartość jest dozwolona i znaczy „jeszcze nieprzypisane".**
        `PartLot.storage_location` jest w Part-DB polem opcjonalnym, więc
        „mam, ale nie wiem gdzie" to pełnoprawny stan, a nie brak danych.
        Tak wygląda część zarejestrowana przed zbudowaniem struktury pudeł
        (ADR-017) — trafia do magazynu i czeka na przypisanie.

        Samo szukanie siedzi w `bom_agent/locations.py`, wspólnie
        z przypisywaniem pojemników (#62); tutaj zostaje tylko przełożenie
        błędu na typ, którego oczekuje zatwierdzanie intake'u.
        """
        try:
            return await resolve_location(self.client, wanted)
        except LocationError as exc:
            raise CommitError(str(exc)) from exc

    def match(
        self, spec: PartSpec, parts: list[JsonDict]
    ) -> tuple[JsonDict | None, list[JsonDict]]:
        """Zwróć (trafienie pewne, kandydaci do rozstrzygnięcia).

        Deduplikacja z 06-intake ma trzy wyjścia, nie dwa:

        * **znaleziona** — ta sama znormalizowana **nazwa** → dokładamy stan,
          nie tworzymy duplikatu;
        * **podobna, ale niepewna** — ten sam MPN przy rozbieżnej nazwie
          (ADR-019), wspólny rozpoznawalny człon (`esp32`, `max7219`) albo
          jedna nazwa zawiera drugą → **pytamy**;
        * **nieznaleziona** → nowa część.

        Tożsamością jest nazwa, nie MPN: MPN identyfikuje układ scalony, a
        dziesiątki różnych modułów dzielą jeden `TP4056`/`MAX7219` (#70). Sam
        zgodny MPN zostaje więc silnym sygnałem podobieństwa, nie kluczem
        scalającym.

        Bez środkowego przypadku po trzech kartonach baza ma `ESP32 DevKit`,
        `ESP32-WROOM-32 DevKit v1` i `esp32 devkit` jako trzy różne części,
        a `check_bom` przestaje działać. Zgadywanie na siłę byłoby jednak
        gorsze: scalenie `AMS1117-3.3` z `AMS1117-5.0` to inne napięcie
        w układzie, więc rozstrzyga człowiek.
        """
        if spec.merge_into:
            forced = next(
                (part for part in parts if part["@id"] == spec.merge_into), None
            )
            if forced is None:
                raise CommitError(
                    f"merge_into={spec.merge_into!r} nie wskazuje na istniejącą część"
                )
            return forced, []

        # Tożsamością części jest jej NAZWA, nie MPN (ADR-019, #70). MPN
        # identyfikuje układ scalony na płytce, a dziesiątki różnych modułów
        # dzielą jeden układ (`TP4056`, `MAX7219`). Sam zgodny MPN robi więc
        # z części kandydata do rozstrzygnięcia, a nie pewne trafienie —
        # inaczej ładowarka z zabezpieczeniem scala się z ładowarką bez.
        name_key = normalize(spec.name)
        mpn_key = normalize(spec.mpn) if spec.mpn else ""

        spec_tokens = tokens(spec.name) | tokens(spec.mpn)
        similar: list[JsonDict] = []

        for part in parts:
            part_name = part.get("name") or ""
            part_mpn = part.get("manufacturer_product_number") or ""

            part_name_key = normalize(part_name)

            if name_key and name_key == part_name_key:
                return part, []

            if spec.force_new:
                continue

            part_mpn_key = normalize(part_mpn)
            same_mpn = bool(mpn_key) and mpn_key == part_mpn_key
            part_tokens = tokens(part_name) | tokens(part_mpn)
            contained = (
                bool(name_key)
                and bool(part_name_key)
                and (name_key in part_name_key or part_name_key in name_key)
            )
            if same_mpn or (spec_tokens & part_tokens) or contained:
                similar.append(part)

        return None, similar

    async def resolve_photo_type(self) -> str:
        """IRI typu załącznika na zdjęcia; utwórz, jeśli instancja go nie ma.

        Świeża instalacja Part-DB ma „Zdjęcie" i „Datasheet", ale to dane,
        nie schemat — ktoś mógł je przemianować albo skasować. Zakładanie
        brakującego typu jest tu bezpieczne, bo typ załącznika to etykieta
        porządkowa, a nie pojemnik w warsztacie (którego nie wymyślamy).
        """
        wanted = {"zdjęcie", "zdjecie", "picture", "image", "photo"}
        types = await self.client.list_attachment_types()
        for attachment_type in types:
            name = (attachment_type.get("name") or "").strip().casefold()
            if name in wanted:
                return str(attachment_type["@id"])
        created = await self.client.create("attachment_types", {"name": "Zdjęcie"})
        return str(created["@id"])

    async def attach_photos(
        self, part_iri: str, photos: list[SourcePhoto]
    ) -> list[str]:
        """Podepnij ujęcia z kolejki do części; pierwsze zostaje miniaturą.

        Part-DB dostaje sam **odnośnik** (`url`), nie kopię pliku — zdjęcie
        i tak leży na dysku `bom-agent` i jest wystawione pod tym adresem,
        a druga kopia oznaczałaby dwa miejsca do backupu i do skasowania.

        `master_picture_attachment` ustawiamy, bo bez tego lista części
        w Part-DB pokazuje pustą ramkę — a cała wartość tej funkcji polega
        na tym, żeby rozpoznać sztukę wzrokiem, bez otwierania rekordu.

        **Nazwa załącznika musi być unikalna w obrębie części** — Part-DB
        odpowiada `422: name: This value is already used` (zmierzone na
        v2.15). Numer ujęcia w kolejce siedzi w nazwie i załatwia to sam,
        także wtedy, gdy jedna pozycja niesie kilka ujęć (#66).
        """
        if not photos:
            return []

        photo_type = await self.resolve_photo_type()
        refs: list[str] = []
        for photo in photos:
            attachment = await self.client.create_attachment(
                {
                    "name": photo.label,
                    "attachment_type": photo_type,
                    "element": part_iri,
                    "url": photo.url,
                }
            )
            refs.append(str(attachment["@id"]))

        # Miniaturą zostaje pierwsze ujęcie: to ono jest tym, które
        # zrobiłeś, patrząc na rzecz — reszta to widok od spodu i detale.
        await self.client.patch(part_iri, {"master_picture_attachment": refs[0]})
        return refs

    async def resolve_category(self, name: str) -> tuple[str | None, str]:
        """Zwróć (IRI istniejącej kategorii, nazwa kategorii do utworzenia)."""
        name = (name or "").strip()
        if not name:
            return None, ""
        for category in await self.client.list_categories():
            if normalize(category.get("name") or "") == normalize(name):
                return category["@id"], ""
            if normalize(category.get("full_path") or "") == normalize(name):
                return category["@id"], ""
        return None, name

    # --- plan i wykonanie --------------------------------------------------

    async def plan(
        self,
        specs: list[PartSpec],
        location: str = "",
        photos: list[SourcePhoto] | None = None,
    ) -> CommitPlan:
        resolved_location = await self.resolve_location(location)
        existing = await self.client.list_parts()

        frames = list(photos or [])
        planned = []
        for spec in specs:
            part, similar = self.match(spec, existing)
            _, new_category = (
                (None, "") if part else await self.resolve_category(spec.category)
            )
            # Wycinek sprawdzamy niezależnie od tego, czy część jest nowa:
            # błędny numer ujęcia to pomyłka agenta i ma się wydać w planie,
            # a nie schować za tym, że część już istniała.
            for_part = self._photos_for(spec, frames)
            planned.append(
                PlannedPart(
                    spec=spec,
                    existing_part=part,
                    new_category=new_category,
                    similar=similar,
                    # Zdjęcie dokładamy tylko przy nowej części — patrz
                    # `execute()`.
                    photos=[] if part else for_part,
                )
            )
        return CommitPlan(location=resolved_location, parts=planned, photos=frames)

    def _photos_for(
        self, spec: PartSpec, frames: list[SourcePhoto]
    ) -> list[SourcePhoto]:
        """Obrazy dla jednej części: całe ujęcia albo sam jej wycinek (#72).

        Bez wycinka zostaje dotychczasowe zachowanie — część dostaje
        wszystkie ujęcia pozycji, bo wszystkie pokazują ją samą (#66).

        Z wycinkiem dostaje **tylko** wycinek. Doklejenie obok niego całego
        kadru wyglądałoby na hojność, a byłoby szkodą: przy pięciu płytkach
        na blacie do każdej z pięciu części trafiłoby to samo zdjęcie
        czterech obcych modułów i nie dałoby się na liście rozpoznać, która
        część jest która.
        """
        if not frames:
            return []
        if spec.crop is None:
            return list(frames)
        if spec.shot > len(frames):
            raise CommitError(
                f"Część {spec.name!r} bierze wycinek z ujęcia {spec.shot}, "
                f"a pozycja ma ich {len(frames)}"
            )
        return [frames[spec.shot - 1].cropped(spec.crop)]

    async def execute(self, plan: CommitPlan) -> list[str]:
        """Wykonaj plan. Zwróć listę IRI utworzonych/zmienionych zasobów.

        Kolejność ma znaczenie: najpierw kategoria, potem część, na końcu
        stan. Przy błędzie w środku to, co już powstało, zostaje —
        i dlatego IRI wracają na zewnątrz, żeby dało się to cofnąć.
        """
        if plan.undecided:
            names = ", ".join(item.spec.name for item in plan.undecided)
            raise CommitError(
                f"Nierozstrzygnięta deduplikacja: {names}. "
                "Zapytaj użytkownika, czy to ta sama część co podobna w bazie, "
                "i podaj merge_into=<IRI> albo force_new=true. "
                "Zapis na ślepo dołożyłby duplikat albo scalił dwie różne części."
            )

        refs: list[str] = []
        location_iri = plan.location["@id"] if plan.location else None

        for item in plan.parts:
            spec = item.spec
            part = item.existing_part

            if part is None:
                category_iri, missing = await self.resolve_category(spec.category)
                if missing:
                    category = await self.client.create("categories", {"name": missing})
                    category_iri = category["@id"]
                    refs.append(category_iri)
                if not category_iri:
                    raise CommitError(
                        f"Część {spec.name!r} nie ma kategorii, a Part-DB jej wymaga"
                    )

                payload: JsonDict = {"name": spec.name, "category": category_iri}
                if spec.description:
                    payload["description"] = spec.description
                if spec.mpn:
                    payload["manufacturer_product_number"] = spec.mpn
                if spec.needs_review:
                    payload["needs_review"] = True
                if spec.note:
                    payload["comment"] = spec.note
                part = await self.client.create_part(payload)
                refs.append(part["@id"])

                # Zdjęcie dokładamy tylko przy **nowej** części. Przy każdym
                # kolejnym zapisie tej samej rzeczy dołożyłoby to dziesiąte
                # ujęcie tego samego modułu — pierwszy raz niesie informację,
                # dziesiąty jest już śmieciem.
                if item.photos:
                    try:
                        refs += await self.attach_photos(part["@id"], item.photos)
                    except PartDbError as exc:
                        # Stan magazynu jest już zapisany i to on ma wartość;
                        # brak ilustracji zgłaszamy, ale nie wywracamy commitu.
                        plan.warnings.append(
                            f"{spec.name}: nie udało się podpiąć ujęć — {exc}"
                        )
            else:
                if spec.needs_review and not part.get("needs_review"):
                    await self.client.patch(part["@id"], {"needs_review": True})
                # Wycinek przy istniejącej części zostaje niewykorzystany —
                # zdjęcie dokładamy tylko przy pierwszym zapisie. Mówimy to
                # wprost, bo agent go wskazał i miałby prawo sądzić, że
                # trafił do bazy.
                if spec.crop is not None:
                    plan.warnings.append(
                        f"{spec.name}: część już istnieje, więc wycinek "
                        f"{spec.crop.as_param()} nie został podpięty — "
                        "zdjęcie dostaje tylko nowa część."
                    )

            # `storage_location` pomijamy, gdy nie ma przypisania — Part-DB
            # dopuszcza stan bez lokalizacji, a to jest uczciwsze niż
            # wpisanie pierwszego lepszego pojemnika (ADR-017).
            lot_payload: JsonDict = {
                "part": part["@id"],
                "amount": spec.amount,
                "instock_unknown": spec.instock_unknown,
                "description": spec.note or "",
            }
            if location_iri:
                lot_payload["storage_location"] = location_iri

            lot = await self.client.create_part_lot(lot_payload)
            refs.append(lot["@id"])

        return refs
