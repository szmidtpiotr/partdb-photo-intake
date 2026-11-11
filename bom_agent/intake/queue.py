"""Kolejka intake — trwała i asynchroniczna (06-intake).

Fotografujesz w sobotę przy sortowaniu, przetwarzasz z agentem w niedzielę.
Telefon nigdy nie czeka na agenta, więc kolejka musi przeżyć restart
kontenera i wyłączenie telefonu.

Stany: `pending` → `processing` → `committed` albo `rejected`.
Zdjęcia odrzucone **zostają na dysku** — najczęstszą przyczyną odrzucenia
jest nieczytelne ujęcie, a wtedy chcesz zobaczyć, co poszło nie tak, zanim
zrobisz drugie.

Pozycja może mieć **kilka ujęć** (#66): moduł fotografuje się z wierzchu,
od spodu i z bliska na sitodruk, a to nadal jeden element. Dodatkowe ujęcia
są wierszami wskazującymi na pozycję główną (`parent_id`) i nigdy nie
pokazują się osobno — inaczej trzy zdjęcia jednego modułu skończyłyby się
trzema zapisami i potrójnym stanem w magazynie.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..db import Database

Kind = Literal["photo", "voice", "manual"]
IntakeStatus = Literal["pending", "processing", "committed", "rejected"]

KINDS: tuple[Kind, ...] = ("photo", "voice", "manual")
STATUSES: tuple[IntakeStatus, ...] = ("pending", "processing", "committed", "rejected")


class IntakeError(RuntimeError):
    """Operacja na kolejce jest niedozwolona albo pozycja nie istnieje."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Shot:
    """Jedno ujęcie pozycji: zdjęcie główne albo dołożone przyciskiem `+`."""

    id: int
    payload_path: str

    @property
    def url(self) -> str:
        return f"/api/intake/{self.id}/photo"


@dataclass(frozen=True)
class IntakeItem:
    id: int
    kind: Kind
    status: IntakeStatus
    created_at: str
    payload_path: str | None = None
    payload_text: str | None = None
    location_hint: str | None = None
    result: dict[str, Any] | list[Any] | None = None
    partdb_refs: list[str] | None = None
    note: str | None = None
    processed_at: str | None = None
    parent_id: int | None = None
    # „Rozpoznaj to" wciśnięte na telefonie (#74). Automat bierze wyłącznie
    # oznaczone pozycje — patrz ADR-021.
    recognize_requested_at: str | None = None
    # Wszystkie ujęcia tej pozycji, główne jako pierwsze. Wypełnia je
    # kolejka przy odczycie — `from_row` widzi tylko jeden wiersz.
    shots: tuple[Shot, ...] = ()

    @property
    def frames(self) -> tuple[Shot, ...]:
        """Ujęcia tej pozycji — a przy zwykłym, pojedynczym zdjęciu ono samo.

        Konsumenci pytają o to zamiast o `payload_path`, żeby jedna rzecz
        sfotografowana raz i sfotografowana czterokrotnie obsługiwała się
        tym samym kodem.
        """
        if self.shots:
            return self.shots
        if self.kind == "photo" and self.payload_path:
            return (Shot(id=self.id, payload_path=self.payload_path),)
        return ()

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> IntakeItem:
        return cls(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            created_at=row["created_at"],
            payload_path=row["payload_path"],
            payload_text=row["payload_text"],
            location_hint=row["location_hint"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            partdb_refs=json.loads(row["partdb_refs"]) if row["partdb_refs"] else None,
            note=row["note"],
            processed_at=row["processed_at"],
            parent_id=row["parent_id"],
            recognize_requested_at=row["recognize_requested_at"],
        )

    def part_records(self) -> list[dict[str, Any]]:
        """Części powstałe z tej pozycji: nazwa + numer rekordu w Part-DB (#69).

        Nazwa jest w `result` — to, co agent rozpoznał. IRI utworzonych części
        siedzą w `partdb_refs` obok kategorii i stanów; wyławiamy z nich te
        wskazujące na rekord części (`…/parts/N`).

        Numer doklejamy tylko wtedy, gdy liczba nowych części zgadza się
        z liczbą nazw. Pozycja, w której część *scalono* w istniejącą, nie ma
        w tym zapisie własnego rekordu części — wtedy zgadywanie po kolejności
        podpięłoby link do złej części, więc zostaje sama nazwa (i tak to po
        niej poznajesz rzecz). Link buduje warstwa web z publicznego adresu
        Part-DB.
        """
        names = [
            record["name"]
            for record in (self.result or [])
            if isinstance(record, dict) and record.get("name")
        ]
        part_ids: list[int] = []
        for ref in self.partdb_refs or []:
            match = re.search(r"/parts/(\d+)$", str(ref))
            if match:
                part_ids.append(int(match.group(1)))

        aligned = len(part_ids) == len(names)
        return [
            {"name": name, "part_id": part_ids[i] if aligned else None}
            for i, name in enumerate(names)
        ]

    def as_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "location_hint": self.location_hint,
            "payload_text": self.payload_text,
            "note": self.note,
            "processed_at": self.processed_at,
            "result": self.result,
            "partdb_refs": self.partdb_refs,
            "recognize_requested_at": self.recognize_requested_at,
            # Nazwy powstałych części z linkiem do rekordu — archiwum PWA (#69)
            # pokazuje „HC-SR04 → Part-DB", nie surowy numer rekordu.
            "created_parts": self.part_records(),
        }
        # Ścieżka na dysku serwera nie mówi nic klientowi i niepotrzebnie
        # zdradza układ katalogów — PWA dostaje adres podglądu.
        if self.kind == "photo":
            data["photo_url"] = f"/api/intake/{self.id}/photo"
            data["shots"] = [{"id": shot.id, "url": shot.url} for shot in self.frames]
        return data


class IntakeQueue:
    """Operacje na kolejce. Cała wiedza o stanach siedzi tutaj."""

    def __init__(self, db: Database, photo_dir: Path | str):
        self.db = db
        self.photo_dir = Path(photo_dir)

    # --- zapis -------------------------------------------------------------

    async def add_photo(
        self,
        content: bytes,
        *,
        suffix: str = ".jpg",
        location_hint: str | None = None,
        parent_id: int | None = None,
    ) -> IntakeItem:
        """Zapisz zdjęcie na dysku i dopisz pozycję do kolejki.

        Plik ląduje na dysku, nie w bazie: zdjęcia z telefonu to megabajty,
        a SQLite ma trzymać kolejkę, nie magazyn plików.

        `parent_id` dokłada **kolejne ujęcie tej samej rzeczy** (#66) zamiast
        nowej pozycji. Zwracana jest wtedy pozycja główna, bo to ona jest
        elementem — ujęcie samo w sobie niczym nie jest.
        """
        if not content:
            raise IntakeError("Puste zdjęcie — nie ma czego kolejkować")

        parent = await self._resolve_parent(parent_id) if parent_id else None

        self.photo_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        path = self.photo_dir / f"{stamp}{suffix}"
        path.write_bytes(content)

        # Ujęcie dziedziczy lokalizację pozycji głównej: opisuje tę samą
        # rzecz, więc druga podpowiedź mogłaby się z pierwszą kłócić.
        hint = parent.location_hint if parent else _clean(location_hint)

        item_id = await self.db.aexecute(
            "INSERT INTO intake_item"
            " (kind, payload_path, location_hint, created_at, parent_id)"
            " VALUES ('photo', ?, ?, ?, ?)",
            (str(path), hint, _now(), parent.id if parent else None),
        )
        return await self.get(parent.id if parent else item_id)

    async def set_photo_note(self, item_id: int, text: str) -> IntakeItem:
        """Dopisz do zdjęcia notatkę użytkownika — np. „BC557, 10 szt".

        Zdjęcie nie mówi, ile sztuk leży w woreczku ani co jest pod naklejką,
        a użytkownik to wie, trzymając rzecz w ręce (#75). Notatka trafia do
        `payload_text`, które przy zdjęciu było puste; agent czyta ją razem
        z obrazem. Pusty tekst kasuje notatkę.

        Notatka wchodzi na pozycję główną, tak jak zlecenie rozpoznania:
        ujęcie nie jest osobną rzeczą.
        """
        item = await self.get(item_id)
        if item.parent_id is not None:
            item = await self.get(item.parent_id)
        if item.kind != "photo":
            raise IntakeError(
                f"Pozycja {item.id} to {item.kind} — jej treść jest opisem, "
                "notatkę dopisuje się do zdjęć"
            )
        if item.status != "pending":
            raise IntakeError(
                f"Pozycja {item.id} jest w stanie {item.status!r} — "
                "notatkę dopisuje się do tego, co jeszcze czeka"
            )
        await self.db.aexecute(
            "UPDATE intake_item SET payload_text = ? WHERE id = ?",
            (text.strip() or None, item.id),
        )
        return await self.get(item.id)

    async def set_location_hint(self, item_id: int, text: str) -> IntakeItem:
        """Wskaż pudełko, do którego rzecz trafi po zatwierdzeniu (#77).

        Użytkownik wie, gdzie odłoży część, zanim agent ją rozpozna — a bez
        tego każda nowa część lądowała w „Bez pudła" i trzeba ją było
        przypisywać drugi raz. To podpowiedź, nie stan bazy (ADR-017):
        przy zatwierdzaniu nieznane pudełko kończy się zapisem bez
        lokalizacji, nie odmową. Pusty tekst kasuje podpowiedź.

        Podpowiedź wchodzi na pozycję główną, tak jak notatka.
        """
        item = await self.get(item_id)
        if item.parent_id is not None:
            item = await self.get(item.parent_id)
        if item.status != "pending":
            raise IntakeError(
                f"Pozycja {item.id} jest w stanie {item.status!r} — "
                "pudełko wskazuje się temu, co jeszcze czeka"
            )
        await self.db.aexecute(
            "UPDATE intake_item SET location_hint = ? WHERE id = ?",
            (_clean(text), item.id),
        )
        return await self.get(item.id)

    async def _resolve_parent(self, parent_id: int) -> IntakeItem:
        """Sprawdź, czy do tej pozycji wolno dołożyć ujęcie."""
        parent = await self.get(parent_id)
        # Ujęcie dołożone do ujęcia to nadal ta sama rzecz — wchodzi pod
        # pozycję główną, zamiast budować drzewo, którego nikt nie chce.
        if parent.parent_id is not None:
            parent = await self.get(parent.parent_id)
        if parent.kind != "photo":
            raise IntakeError(
                f"Pozycja {parent.id} to {parent.kind}, nie zdjęcie — "
                "nie ma do czego dokładać ujęcia"
            )
        if parent.status != "pending":
            raise IntakeError(
                f"Pozycja {parent.id} jest w stanie {parent.status!r} — "
                "ujęcia dokłada się tylko do pozycji czekającej na rozpoznanie"
            )
        return parent

    async def add_text(
        self, text: str, *, kind: Kind = "voice", location_hint: str | None = None
    ) -> IntakeItem:
        text = text.strip()
        if not text:
            raise IntakeError("Pusty tekst — nie ma czego kolejkować")
        if kind not in ("voice", "manual"):
            raise IntakeError(f"Rodzaj {kind!r} nie przyjmuje tekstu")

        item_id = await self.db.aexecute(
            "INSERT INTO intake_item (kind, payload_text, location_hint, created_at)"
            " VALUES (?, ?, ?, ?)",
            (kind, text, _clean(location_hint), _now()),
        )
        return await self.get(item_id)

    # --- odczyt ------------------------------------------------------------

    async def get(self, item_id: int) -> IntakeItem:
        row = await self.db.aquery_one(
            "SELECT * FROM intake_item WHERE id = ?", (item_id,)
        )
        if row is None:
            raise IntakeError(f"Pozycja {item_id} nie istnieje w kolejce")
        items = await self._with_shots([IntakeItem.from_row(row)])
        return items[0]

    async def list(
        self, *, status: IntakeStatus | None = None, limit: int = 50
    ) -> list[IntakeItem]:
        """Najnowsze na górze — na telefonie liczy się to, co przed chwilą.

        Dodatkowe ujęcia (`parent_id`) nie są tu pozycjami: wchodzą w skład
        swojej pozycji głównej jako `shots`.
        """
        if status is not None and status not in STATUSES:
            raise IntakeError(f"Nieznany stan: {status!r}")
        sql = "SELECT * FROM intake_item WHERE parent_id IS NULL"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.aquery(sql, params)
        return await self._with_shots([IntakeItem.from_row(row) for row in rows])

    async def counts(self) -> dict[str, int]:
        """Liczymy elementy, nie zdjęcia — ujęcia jednej rzeczy to jedna
        pozycja do rozpoznania, więc doliczanie ich zawyżałoby kolejkę."""
        rows = await self.db.aquery(
            "SELECT status, COUNT(*) AS n FROM intake_item"
            " WHERE parent_id IS NULL GROUP BY status"
        )
        counts = {status: 0 for status in STATUSES}
        counts.update({row["status"]: row["n"] for row in rows})
        return counts

    async def _with_shots(self, items: list[IntakeItem]) -> list[IntakeItem]:
        """Dopnij do pozycji zdjęciowych listę ujęć — główne jako pierwsze."""
        photos = [item for item in items if item.kind == "photo" and item.payload_path]
        if not photos:
            return items

        parents = [item.id for item in photos if item.parent_id is None]
        extra: dict[int, list[Shot]] = {}
        if parents:
            placeholders = ",".join("?" * len(parents))
            # W zapytaniu ląduje wyłącznie ciąg znaków „?", a same numery
            # jadą jako parametry — SQLite nie przyjmuje listy w jednym.
            sql = (
                "SELECT id, parent_id, payload_path FROM intake_item"  # noqa: S608
                f" WHERE parent_id IN ({placeholders}) ORDER BY id"
            )
            rows = await self.db.aquery(sql, parents)
            for row in rows:
                if row["payload_path"]:
                    extra.setdefault(row["parent_id"], []).append(
                        Shot(id=row["id"], payload_path=row["payload_path"])
                    )

        result = []
        for item in items:
            if item.kind != "photo" or not item.payload_path:
                result.append(item)
                continue
            shots = [Shot(id=item.id, payload_path=item.payload_path)]
            shots += extra.get(item.id, [])
            result.append(replace(item, shots=tuple(shots)))
        return result

    # --- usuwanie ----------------------------------------------------------

    async def delete(self, item_id: int) -> IntakeItem:
        """Usuń pozycję razem z plikiem zdjęcia.

        To jest co innego niż `reject()`. Odrzucenie mówi „to ujęcie jest
        nieudane, zostaw je jako dowód"; usunięcie mówi „to w ogóle nie
        powinno tu trafić" — zdjęcie palca, przypadkowy plik z galerii,
        podwójna wysyłka. Trzymanie takich śmieci nie służy niczemu.

        **Pozycji `committed` nie usuwamy.** Ona wskazuje na części, które
        powstały w Part-DB (`partdb_refs`), i jest jedynym śladem
        pozwalającym cofnąć zapis. Skasowanie jej zostawiłoby części
        w magazynie bez informacji, skąd się wzięły.

        Usunięcie pozycji z kilkoma ujęciami zabiera **wszystkie** — to jeden
        element, a osierocone ujęcie nie opisywałoby już niczego. Pojedyncze
        nieudane ujęcie kasuje się przez jego własny numer.
        """
        item = await self.get(item_id)
        if item.status == "committed":
            raise IntakeError(
                f"Pozycja {item_id} jest zatwierdzona i wskazuje na zapisane "
                "części — nie usuwam, bo to jedyny ślad pozwalający je cofnąć."
            )

        doomed = [item.id, *(shot.id for shot in item.shots if shot.id != item.id)]
        paths = [Path(shot.payload_path) for shot in item.shots]
        if item.payload_path and not paths:
            paths = [Path(item.payload_path)]
        placeholders = ",".join("?" * len(doomed))

        # Propozycje wskazują na pozycję kluczem obcym, więc bez ich
        # usunięcia baza odmawia. Zatwierdzona propozycja to część zapisana
        # w Part-DB — ten sam ślad co pozycja `committed`, więc też chroni.
        approved = await self.db.aquery(
            "SELECT COUNT(*) AS n FROM intake_proposal"  # noqa: S608
            f" WHERE item_id IN ({placeholders}) AND status = 'approved'",
            doomed,
        )
        if approved and approved[0]["n"]:
            raise IntakeError(
                f"Z pozycji {item_id} zapisano już części w magazynie — nie "
                "usuwam, bo to jedyny ślad pozwalający je cofnąć."
            )
        # Najpierw baza, potem pliki: odwrotna kolejność przy błędzie bazy
        # zostawiała wiersz wskazujący na skasowane zdjęcie.
        await self.db.aexecute(
            f"DELETE FROM intake_proposal WHERE item_id IN ({placeholders})",  # noqa: S608
            doomed,
        )
        await self.db.aexecute(
            f"DELETE FROM intake_item WHERE id IN ({placeholders})",  # noqa: S608
            doomed,
        )

        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover — prawa dostępu na hoście
                raise IntakeError(
                    f"Nie udało się usunąć pliku {path.name}: {exc}"
                ) from exc
        return item

    # --- zlecenie rozpoznania ----------------------------------------------

    async def request_recognition(
        self, item_id: int, *, wanted: bool = True
    ) -> IntakeItem:
        """Oznacz pozycję jako zleconą do rozpoznania — albo zdejmij oznaczenie.

        To jest **jawne zlecenie**, nie zgoda na przemiał kolejki (ADR-021).
        Automat (#75) bierze wyłącznie oznaczone pozycje, bo zdjęcie
        nieoznaczone bywa niekompletne: użytkownik zamierza dołożyć ujęcie
        spodu (#66), a rozpoznanie połowy modułu kosztuje tyle samo, co
        rozpoznanie całego.

        Oznaczenie wchodzi na pozycję główną. Ujęcie nie jest osobną rzeczą,
        więc nie może mieć własnego zlecenia.
        """
        item = await self.get(item_id)
        if item.parent_id is not None:
            item = await self.get(item.parent_id)
        if item.status != "pending":
            raise IntakeError(
                f"Pozycja {item.id} jest w stanie {item.status!r} — "
                "rozpoznanie zleca się temu, co jeszcze czeka"
            )
        await self.db.aexecute(
            "UPDATE intake_item SET recognize_requested_at = ? WHERE id = ?",
            (_now() if wanted else None, item.id),
        )
        return await self.get(item.id)

    async def requested(self, limit: int = 50) -> list[IntakeItem]:
        """Pozycje zlecone do rozpoznania i NIEROZPOZNANE, najstarsze pierwsze.

        Kolejność odwrotna niż w `list()`: tam liczy się to, co przed chwilą
        wpadło z telefonu, a tu — żeby najdłużej czekające nie zostało
        w tyle, gdy zleceń jest więcej niż jedno przebiegnięcie automatu.

        **Pozycje z propozycją odsiewa SQL, nie kod woław‑cy** — i to jest
        cała różnica. Wcześniej `LIMIT` szedł przed tym filtrem, więc okno
        ośmiu najstarszych zleceń wypełniały pozycje, które już czekały na
        klik użytkownika, a tych faktycznie do rozpoznania nie było w nim
        nigdy. Zmierzone na instancji: 19 zleconych, 8 z propozycją, 11 do
        wzięcia — i automat przez siedem godzin, co dziesięć minut, meldował
        „brak zleceń", bo widział wyłącznie te osiem. Zakleszczenie
        odblokowywało się dopiero po odklikaniu propozycji przez człowieka,
        czyli dokładnie wtedy, gdy automat miał wyręczać człowieka.
        """
        rows = await self.db.aquery(
            "SELECT * FROM intake_item WHERE parent_id IS NULL"
            " AND status = 'pending' AND recognize_requested_at IS NOT NULL"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM intake_proposal"
            "   WHERE intake_proposal.item_id = intake_item.id"
            "     AND intake_proposal.status = 'pending'"
            " )"
            " ORDER BY recognize_requested_at ASC, id ASC LIMIT ?",
            (max(1, limit),),
        )
        return await self._with_shots([IntakeItem.from_row(row) for row in rows])

    # --- przejścia stanów --------------------------------------------------

    async def claim(self, item_id: int) -> IntakeItem:
        """`pending` → `processing`. Agent bierze pozycję do rozpoznania."""
        return await self._transition(item_id, "processing", allowed=("pending",))

    async def commit(
        self, item_id: int, *, result: Any = None, partdb_refs: list[str] | None = None
    ) -> IntakeItem:
        """→ `committed`. `partdb_refs` to IRI utworzonych rzeczy — ślad
        potrzebny, żeby dało się cofnąć błędny zapis."""
        return await self._transition(
            item_id,
            "committed",
            allowed=("pending", "processing"),
            result=result,
            partdb_refs=partdb_refs,
        )

    async def reject(self, item_id: int, note: str) -> IntakeItem:
        note = note.strip()
        if not note:
            raise IntakeError(
                "Odrzucenie bez powodu jest bezużyteczne — podaj, co jest nie tak"
            )
        return await self._transition(
            item_id, "rejected", allowed=("pending", "processing"), note=note
        )

    async def _transition(
        self,
        item_id: int,
        target: IntakeStatus,
        *,
        allowed: tuple[IntakeStatus, ...],
        result: Any = None,
        partdb_refs: list[str] | None = None,
        note: str | None = None,
    ) -> IntakeItem:
        item = await self.get(item_id)
        if item.status == target:
            return item
        if item.status not in allowed:
            raise IntakeError(
                f"Pozycja {item_id} jest w stanie {item.status!r}; "
                f"przejście do {target!r} jest dozwolone tylko z {allowed}"
            )
        await self.db.aexecute(
            "UPDATE intake_item SET status = ?, processed_at = ?,"
            " result_json = COALESCE(?, result_json),"
            " partdb_refs = COALESCE(?, partdb_refs),"
            " note = COALESCE(?, note)"
            " WHERE id = ?",
            (
                target,
                _now(),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                json.dumps(partdb_refs, ensure_ascii=False) if partdb_refs else None,
                note,
                item_id,
            ),
        )
        # Ujęcia idą za swoją pozycją. Zostawienie ich w `pending` sprawiłoby,
        # że po zatwierdzeniu elementu jego zdjęcia wróciłyby do rozpoznania
        # jako osobne rzeczy — czyli dokładnie ten błąd, który #66 usuwa.
        await self.db.aexecute(
            "UPDATE intake_item SET status = ?, processed_at = ? WHERE parent_id = ?",
            (target, _now(), item_id),
        )
        return await self.get(item_id)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
