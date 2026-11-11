"""Etap propozycji: rozpoznanie zapisuje TUTAJ, nie w Part-DB (ADR-021).

Do #73 rozpoznanie i zapis były jedną operacją, więc akceptacja człowieka
musiała odbyć się w rozmowie z agentem — przy komputerze, nad tekstowym
planem. Rola użytkownika miała się kończyć na zdjęciu, a kończyła się na
czytaniu planu.

Propozycja to ten sam ładunek, który dziś przyjmuje `intake_commit`
(nazwa, ilość, kategoria, wycinek, rozstrzygnięcia dedupu) plus **werdykt
deduplikacji z chwili rozpoznania**. Werdykt jest zapisany, a nie liczony
przy zatwierdzaniu, bo telefon ma pokazać „dołoży stan do *Ładowarka
TP4056*" bez pytania Part-DB przy każdym przewinięciu listy.

Dwie rzeczy, na których to stoi:

* **Propozycja nie rusza magazynu.** Dlatego rozpoznawanie bez nadzoru
  (#75) jest bezpieczne: najgorszy skutek to wiersz do odrzucenia.
* **Zatwierdzenie wykonuje `IntakeCommitter`**, ten sam, którego używa
  `intake_commit`. Zero drugiej ścieżki zapisu — kategorie, deduplikacja,
  wycinki i stany zachowują się identycznie, niezależnie od tego, czy
  zatwierdza agent w czacie, czy palec na telefonie.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from ..db import Database
from .commit import CommitError, IntakeCommitter, PartSpec, SourcePhoto
from .queue import IntakeItem, IntakeQueue

JsonDict = dict[str, Any]

ProposalStatus = Literal["pending", "approved", "rejected"]
STATUSES: tuple[ProposalStatus, ...] = ("pending", "approved", "rejected")

# Pola, które wolno poprawić z telefonu. Reszta ładunku (wycinek, numer
# ujęcia, rozstrzygnięcia dedupu) powstaje z rozpoznania i zmiana jej
# palcem na ekranie znaczyłaby, że rozpoznanie było do wyrzucenia.
EDITABLE = frozenset(
    {
        "name",
        "amount",
        "bucket",
        "category",
        "description",
        "mpn",
        "manufacturer",
        "note",
        "instock_unknown",
        "needs_review",
        "location",
    }
)


class ProposalError(RuntimeError):
    """Operacja na propozycji jest niedozwolona albo propozycja nie istnieje."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Proposal:
    id: int
    item_id: int
    payload: JsonDict
    preview: JsonDict = field(default_factory=dict)
    status: ProposalStatus = "pending"
    partdb_refs: list[str] | None = None
    note: str | None = None
    created_at: str = ""
    decided_at: str | None = None

    @classmethod
    def from_row(cls, row: JsonDict) -> Proposal:
        return cls(
            id=row["id"],
            item_id=row["item_id"],
            payload=json.loads(row["payload_json"]),
            preview=json.loads(row["preview_json"]) if row["preview_json"] else {},
            status=row["status"],
            partdb_refs=json.loads(row["partdb_refs"]) if row["partdb_refs"] else None,
            note=row["note"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    @property
    def name(self) -> str:
        return str(self.payload.get("name") or "").strip()

    def as_dict(self) -> JsonDict:
        """Postać dla PWA. Miniatura to **wycinek**, nie cały kadr (#72).

        Przy pięciu płytkach z jednego zdjęcia karty z tym samym kadrem
        byłyby nierozróżnialne, a to jedyna rzecz, po której użytkownik
        poznaje, którą propozycję właśnie zatwierdza.
        """
        return {
            "id": self.id,
            "item_id": self.item_id,
            "status": self.status,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "note": self.note,
            "partdb_refs": self.partdb_refs,
            "payload": self.payload,
            "photo_url": self.preview.get("photo_url"),
            "action": self.preview.get("action", "create"),
            "existing": self.preview.get("existing"),
            "location": self.payload.get("location") or self.preview.get("location"),
            "new_category": self.preview.get("new_category") or "",
        }


class ProposalStore:
    """Sam zapis propozycji. Nie dotyka Part-DB ani kolejki."""

    def __init__(self, db: Database):
        self.db = db

    async def create(
        self, item_id: int, entries: list[tuple[JsonDict, JsonDict]]
    ) -> list[Proposal]:
        """Dopisz propozycje do pozycji. `entries` to pary (ładunek, werdykt)."""
        if not entries:
            raise ProposalError("Pusta lista propozycji — nie ma czego zapisać")
        created = []
        for payload, preview in entries:
            if not str(payload.get("name") or "").strip():
                raise ProposalError("Propozycja bez nazwy — nie ma czego pokazać")
            proposal_id = await self.db.aexecute(
                "INSERT INTO intake_proposal"
                " (item_id, payload_json, preview_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    item_id,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(preview, ensure_ascii=False),
                    _now(),
                ),
            )
            created.append(await self.get(proposal_id))
        return created

    async def get(self, proposal_id: int) -> Proposal:
        row = await self.db.aquery_one(
            "SELECT * FROM intake_proposal WHERE id = ?", (proposal_id,)
        )
        if row is None:
            raise ProposalError(f"Propozycja {proposal_id} nie istnieje")
        return Proposal.from_row(row)

    async def list(
        self,
        *,
        item_id: int | None = None,
        status: ProposalStatus | None = "pending",
        limit: int = 200,
    ) -> list[Proposal]:
        if status is not None and status not in STATUSES:
            raise ProposalError(f"Nieznany stan propozycji: {status!r}")
        sql = "SELECT * FROM intake_proposal WHERE 1=1"
        params: list[Any] = []
        if item_id is not None:
            sql += " AND item_id = ?"
            params.append(item_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY item_id DESC, id ASC LIMIT ?"
        params.append(limit)
        return [Proposal.from_row(row) for row in await self.db.aquery(sql, params)]

    async def items_with_pending(self) -> set[int]:
        """Pozycje, które mają choć jedną propozycję czekającą na decyzję.

        Po tym rozróżniamy „czeka na rozpoznanie" od „czeka na Twoje OK" —
        bez dokładania stanu do `intake_item` (ADR-021).
        """
        rows = await self.db.aquery(
            "SELECT DISTINCT item_id FROM intake_proposal WHERE status = 'pending'"
        )
        return {row["item_id"] for row in rows}

    async def counts(self) -> dict[str, int]:
        rows = await self.db.aquery(
            "SELECT status, COUNT(*) AS n FROM intake_proposal GROUP BY status"
        )
        counts = {status: 0 for status in STATUSES}
        counts.update({row["status"]: row["n"] for row in rows})
        return counts

    async def edit(self, proposal_id: int, changes: JsonDict) -> Proposal:
        """Popraw ładunek propozycji. Tylko pola z `EDITABLE`.

        Poprawka nazwy unieważnia zapisany werdykt deduplikacji: „dołoży
        stan do *X*" dotyczyło innej nazwy, a zostawienie go pokazywałoby
        na telefonie coś, co przy zatwierdzaniu już nie nastąpi.
        """
        proposal = await self.get(proposal_id)
        if proposal.status != "pending":
            raise ProposalError(
                f"Propozycja {proposal_id} jest już {proposal.status} — nie edytuję"
            )

        unknown = set(changes) - EDITABLE
        if unknown:
            raise ProposalError(
                f"Tych pól nie wolno zmieniać z telefonu: {', '.join(sorted(unknown))}"
            )

        payload = dict(proposal.payload)
        payload.update({k: v for k, v in changes.items() if v is not None})
        if not str(payload.get("name") or "").strip():
            raise ProposalError("Nazwa nie może zostać pusta")

        preview = dict(proposal.preview)
        if "name" in changes and changes["name"] != proposal.payload.get("name"):
            preview["action"] = "create"
            preview.pop("existing", None)
            preview["stale"] = (
                "nazwa poprawiona po rozpoznaniu — deduplikacja zostanie "
                "policzona ponownie przy zatwierdzaniu"
            )

        await self.db.aexecute(
            "UPDATE intake_proposal SET payload_json = ?, preview_json = ?"
            " WHERE id = ?",
            (
                json.dumps(payload, ensure_ascii=False),
                json.dumps(preview, ensure_ascii=False),
                proposal_id,
            ),
        )
        return await self.get(proposal_id)

    async def claim(self, proposal_id: int) -> bool:
        """Zabierz propozycję do zapisu. `False` = ktoś już ją zabrał.

        Sprawdzenie „czy jeszcze pending" i zapis to dwie różne chwile —
        zmierzone na żywej instancji: dwa dotknięcia ✓ przeszły oba
        sprawdzenia i część dostała dwa stany po jednej sztuce. Jedno
        atomowe `UPDATE ... WHERE claimed_at IS NULL` usuwa ten wyścig,
        bo drugi zapis nie ma czego zabrać.
        """
        rows = await self.db.aquery(
            "UPDATE intake_proposal SET claimed_at = ?"
            " WHERE id = ? AND status = 'pending' AND claimed_at IS NULL"
            " RETURNING id",
            (_now(), proposal_id),
        )
        return bool(rows)

    async def release(self, proposal_id: int) -> None:
        """Oddaj propozycję, gdy zapis się nie udał — inaczej zostałaby
        zabrana na zawsze i nie dałoby się jej ponowić."""
        await self.db.aexecute(
            "UPDATE intake_proposal SET claimed_at = NULL WHERE id = ?",
            (proposal_id,),
        )

    async def decide(
        self,
        proposal_id: int,
        status: ProposalStatus,
        *,
        refs: list[str] | None = None,
        note: str | None = None,
    ) -> Proposal:
        proposal = await self.get(proposal_id)
        if proposal.status != "pending":
            raise ProposalError(f"Propozycja {proposal_id} jest już {proposal.status}")
        await self.db.aexecute(
            "UPDATE intake_proposal SET status = ?, decided_at = ?,"
            " partdb_refs = COALESCE(?, partdb_refs), note = COALESCE(?, note)"
            " WHERE id = ?",
            (
                status,
                _now(),
                json.dumps(refs, ensure_ascii=False) if refs else None,
                note,
                proposal_id,
            ),
        )
        return await self.get(proposal_id)


class ProposalReviewer:
    """Zatwierdzanie i odrzucanie — jedyne miejsce, gdzie propozycja rusza bazę."""

    def __init__(
        self,
        store: ProposalStore,
        queue: IntakeQueue,
        committer: IntakeCommitter,
        photo_url: Any,
    ):
        self.store = store
        self.queue = queue
        self.committer = committer
        # Buduje publiczny adres ujęcia z jego numeru. Wstrzykiwany, bo tylko
        # warstwa web i MCP znają `PUBLIC_BASE_URL` (#64).
        self.photo_url = photo_url

    def photos_for(self, item: IntakeItem) -> list[SourcePhoto]:
        return [
            SourcePhoto(url=self.photo_url(shot.id), item_id=shot.id)
            for shot in item.frames
        ]

    async def approve(self, proposal_id: int) -> tuple[Proposal, list[str]]:
        """Zapisz jedną propozycję do Part-DB przez zwykły `IntakeCommitter`."""
        proposal = await self.store.get(proposal_id)
        if proposal.status != "pending":
            raise ProposalError(
                f"Propozycja {proposal_id} jest już {proposal.status} — "
                "nie zapisuję drugi raz"
            )
        # Zabranie PRZED zapisem, nie po. Sprawdzenie stanu i zapis to dwie
        # chwile, a między nimi zmieściło się drugie dotknięcie ✓ — część
        # dostała wtedy dwa stany po jednej sztuce (zmierzone na instancji).
        if not await self.store.claim(proposal_id):
            raise ProposalError(
                f"Propozycja {proposal_id} jest właśnie zapisywana albo już "
                "zapisana — drugie kliknięcie nic nie dokłada."
            )

        try:
            return await self._write(proposal_id, proposal)
        except Exception:
            # Cokolwiek poszło nie tak, propozycja musi wrócić do ruchu —
            # inaczej zostałaby zabrana na zawsze i nie dałoby się ponowić.
            await self.store.release(proposal_id)
            raise

    async def _write(
        self, proposal_id: int, proposal: Proposal
    ) -> tuple[Proposal, list[str]]:
        item = await self.queue.get(proposal.item_id)
        try:
            spec = PartSpec.from_dict(proposal.payload)
        except CommitError as exc:
            raise ProposalError(str(exc)) from exc

        wanted = str(proposal.payload.get("location") or "").strip()
        hint = (item.location_hint or "").strip()
        photos = self.photos_for(item)

        try:
            plan = await self.committer.plan([spec], wanted or hint, photos=photos)
        except CommitError as exc:
            # Podpowiedź z telefonu to plan użytkownika, nie stan bazy
            # (ADR-017) — rejestrujemy bez przypisania, zamiast odmawiać.
            if wanted or not hint:
                raise ProposalError(str(exc)) from exc
            plan = await self.committer.plan([spec], "", photos=photos)

        if plan.undecided:
            candidates = "; ".join(
                f"{part.get('name')} ({part['@id']})"
                for part in plan.undecided[0].similar
            )
            raise ProposalError(
                f"„{proposal.name}” ma w bazie części podobne, a nie takie same: "
                f"{candidates}. Rozstrzygnięcie należy do agenta (merge_into "
                "albo force_new), nie do ekranu telefonu — poproś o ponowne "
                "rozpoznanie tej pozycji."
            )

        try:
            refs = await self.committer.execute(plan)
        except CommitError as exc:
            raise ProposalError(str(exc)) from exc

        approved = await self.store.decide(proposal_id, "approved", refs=refs)
        await self._close_item_if_done(proposal.item_id)
        return approved, refs

    async def reject(self, proposal_id: int, note: str = "") -> Proposal:
        """Wyrzuć propozycję. Powód jest opcjonalny — inaczej niż przy
        odrzuceniu całej pozycji, gdzie zdjęcie zostaje jako dowód."""
        rejected = await self.store.decide(
            proposal_id, "rejected", note=note.strip() or None
        )
        await self._close_item_if_done(rejected.item_id)
        return rejected

    async def approve_item(self, item_id: int) -> tuple[list[Proposal], list[str]]:
        """Zatwierdź wszystkie oczekujące propozycje pozycji, po kolei.

        Pojedynczo, nie hurtem: deduplikacja każdej kolejnej części widzi
        wtedy to, co powstało chwilę wcześniej. Przy zdjęciu grupowym
        (#72) sąsiednie płytki bywają podobne, a plan policzony raz na
        początku nie wiedziałby o pierwszej z nich.
        """
        pending = await self.store.list(item_id=item_id, status="pending")
        if not pending:
            raise ProposalError(
                f"Pozycja {item_id} nie ma propozycji czekających na decyzję"
            )
        done: list[Proposal] = []
        refs: list[str] = []
        for proposal in pending:
            approved, created = await self.approve(proposal.id)
            done.append(approved)
            refs += created
        return done, refs

    async def _close_item_if_done(self, item_id: int) -> None:
        """Gdy nie została żadna propozycja do decyzji — domknij pozycję.

        Zatwierdzone części dają `committed` razem ze śladem `partdb_refs`;
        same odrzucenia dają `rejected`, bo nic nie weszło do magazynu.
        Bez tego pozycja wisiałaby w „Czeka" po zamknięciu wszystkich jej
        propozycji i prosiła o rozpoznanie rzeczy już rozliczonej.
        """
        if await self.store.list(item_id=item_id, status="pending"):
            return

        item = await self.queue.get(item_id)
        if item.status in ("committed", "rejected"):
            return

        approved = await self.store.list(item_id=item_id, status="approved")
        if not approved:
            await self.queue.reject(
                item_id,
                "Wszystkie propozycje odrzucone — nic nie weszło do magazynu.",
            )
            return

        refs: list[str] = []
        for proposal in approved:
            refs += proposal.partdb_refs or []
        await self.queue.commit(
            item_id,
            result=[
                {
                    "name": proposal.name,
                    "amount": proposal.payload.get("amount", 1),
                    "needs_review": bool(proposal.payload.get("needs_review")),
                }
                for proposal in approved
            ],
            partdb_refs=refs,
        )
