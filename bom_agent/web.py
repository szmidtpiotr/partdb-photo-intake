"""FastAPI: `/health`, API kolejki intake i serwowanie PWA.

Podział ról jest z 06-intake: telefon tylko **wrzuca** materiał do kolejki
i nigdy nie czeka na agenta. Rozpoznawanie i zapis do Part-DB dzieją się
później, w sesji z agentem, przez narzędzia MCP.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import warsztat_edit as edit
from .config import Settings, load_settings
from .db import Database
from .intake.commit import IntakeCommitter
from .intake.images import MAX_EDGE, CropBox, CropError, prepare_for_agent
from .intake.proposals import ProposalError, ProposalReviewer, ProposalStore
from .intake.queue import IntakeError, IntakeQueue
from .partdb_client import PartDbClient, PartDbError
from .warsztat import CARD_EDGE, THUMB_EDGE, build_boxes, part_card
from .warsztat import snapshot as warsztat_snapshot

logger = logging.getLogger("bom_agent.web")

# Rozmiary podglądu zdjęcia z kolejki: miniatura listy i zdjęcie karty
# Warsztatu (ADR-022) plus rozmiar, który widzi agent.
# 480 — wycinek na karcie propozycji w zakładce „Dodaj”.
PHOTO_EDGES = {THUMB_EDGE, 480, CARD_EDGE, MAX_EDGE}

PWA_DIR = Path(__file__).resolve().parent.parent / "pwa"

# Telefon wysyła JPEG-a, czasem HEIC (iOS) albo PNG. Listę trzymamy wąską
# celowo: to jest publiczny endpoint w LAN-ie, a przyjmowanie dowolnego
# pliku zamieniłoby kolejkę intake w hosting.
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

# 25 MB — tyle samo, ile przepuszcza reverse proxy (client_max_body_size).
# Rozjazd tych dwóch liczb dałby błąd nginxa zamiast czytelnego komunikatu.
MAX_PHOTO_BYTES = 25 * 1024 * 1024

# Typy, które Android potrafi podać zamiast konkretnego formatu obrazu.
# Wybieraki plików bywają w tym niedbałe — zwłaszcza przy zdjęciach
# pobieranych z chmury — a odrzucenie zdjęcia z powodu pustego nagłówka
# byłoby karaniem użytkownika za cudzy błąd.
VAGUE_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def resolve_image_type(content_type: str, filename: str) -> str | None:
    """Ustal typ obrazu z nagłówka, a gdy ten jest bezużyteczny — z nazwy."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type in ALLOWED_IMAGE_TYPES:
        return content_type
    if content_type not in VAGUE_TYPES:
        return None
    suffix = Path(filename or "").suffix.lower()
    for allowed, extension in ALLOWED_IMAGE_TYPES.items():
        if suffix == extension or (suffix == ".jpeg" and extension == ".jpg"):
            return allowed
    return None


def create_app(
    settings: Settings | None = None,
    client: PartDbClient | None = None,
    queue: IntakeQueue | None = None,
) -> FastAPI:
    """Zbuduj aplikację.

    `client` i `queue` wstrzykuje się w testach; w produkcji powstają
    w lifespanie, razem z migracją bazy kolejki.
    """
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level)
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.partdb = client or PartDbClient(
            settings.partdb_url,
            settings.partdb_api_token,
            timeout=settings.partdb_timeout,
        )
        if queue is not None:
            app.state.queue = queue
            app.state.proposals = ProposalStore(queue.db)
        else:
            database = Database(settings.bom_db_path)
            version = await database.amigrate()
            logger.info(
                "Kolejka intake: %s (schemat v%s)", settings.bom_db_path, version
            )
            app.state.queue = IntakeQueue(database, settings.intake_photo_dir)
            app.state.proposals = ProposalStore(database)
        if not settings.has_partdb_token:
            logger.warning(
                "PARTDB_API_TOKEN jest pusty — /health zgłosi stan degraded, "
                "a każdy zapis do Part-DB skończy się błędem autoryzacji"
            )
        try:
            yield
        finally:
            if owns_client:
                await app.state.partdb.aclose()

    app = FastAPI(
        title="bom-agent",
        version=__version__,
        summary="Zapis, intake i wnioskowanie nad Part-DB",
        lifespan=lifespan,
    )

    @app.exception_handler(IntakeError)
    async def intake_error_handler(_request: Any, exc: IntakeError) -> JSONResponse:
        # 400, nie 500: to są błędy użycia (pusty plik, zły stan), a telefon
        # ma pokazać powód, nie „coś poszło nie tak".
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(ProposalError)
    async def proposal_error_handler(_request: Any, exc: ProposalError) -> JSONResponse:
        # 409, nie 400: propozycja istnieje, ale operacja koliduje ze stanem
        # bazy (już zatwierdzona, dedup wymaga rozstrzygnięcia). Telefon ma
        # pokazać powód, a nie „coś poszło nie tak".
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.get("/health", tags=["diagnostyka"])
    async def health() -> dict[str, Any]:
        """Zawsze 200 — stan warstwy magazynowej jest w treści.

        Zwracanie 5xx, gdy to Part-DB leży, mieszałoby dwa różne pytania:
        „czy usługa żyje" i „czy zależność żyje". Orkiestrator ma wiedzieć,
        że sam `bom-agent` wstał.
        """
        status = await app.state.partdb.ping()
        queue_counts = await app.state.queue.counts()
        return {
            "service": "bom-agent",
            "version": __version__,
            "status": "ok" if status.ok else "degraded",
            "partdb": {
                "url": settings.partdb_url,
                "reachable": status.reachable,
                "authorized": status.authorized,
                "latency_ms": status.latency_ms,
                "total_parts": status.total_parts,
                "error": status.error,
            },
            "intake": queue_counts,
        }

    # --- API kolejki intake -------------------------------------------------

    @app.post("/api/intake/photo", status_code=201, tags=["intake"])
    async def upload_photo(
        photo: UploadFile,
        location_hint: str | None = Form(default=None),
        parent_id: int | None = Form(default=None),
    ) -> dict[str, Any]:
        """Zdjęcie pojemnika z telefonu → kolejka.

        Zwraca od razu, bez rozpoznawania. Rozpoznaje agent, później,
        narzędziem `intake_pending()` (ADR-005).

        `parent_id` znaczy „to kolejne ujęcie tej samej rzeczy" (#66):
        moduł sfotografowany z wierzchu i od spodu ma zostać jednym
        elementem, nie dwoma. Odpowiedzią jest wtedy pozycja główna.
        """
        content_type = resolve_image_type(
            photo.content_type or "", photo.filename or ""
        )
        if content_type is None:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Typ {photo.content_type or 'nieznany'} "
                    f"(plik {photo.filename or 'bez nazwy'}) nie jest obsługiwany. "
                    f"Dozwolone: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}"
                ),
            )

        content = await photo.read()
        if len(content) > MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Zdjęcie ma {len(content) // 1024 // 1024} MB, "
                    f"limit to {MAX_PHOTO_BYTES // 1024 // 1024} MB"
                ),
            )

        item = await app.state.queue.add_photo(
            content,
            suffix=ALLOWED_IMAGE_TYPES[content_type],
            location_hint=location_hint,
            parent_id=parent_id,
        )
        logger.info(
            "Intake: zdjęcie #%s (%s kB, ujęć: %s, lokalizacja: %s)",
            item.id,
            len(content) // 1024,
            len(item.shots) or 1,
            item.location_hint or "brak",
        )
        return item.as_dict()

    @app.post("/api/intake/voice", status_code=201, tags=["intake"])
    async def upload_voice(
        text: str = Form(...), location_hint: str | None = Form(default=None)
    ) -> dict[str, Any]:
        """Dyktowanie z klawiatury telefonu → kolejka (ADR-006).

        Tekst nie jest tu parsowany. Rozbiciem `"kuweta B3, dużo 100nF"`
        na rekordy zajmuje się agent, bo to wymaga wiedzy o elektronice,
        a nie wyrażeń regularnych.
        """
        item = await app.state.queue.add_text(text, location_hint=location_hint)
        # Tekst jest od razu zleceniem (#75). Zdjęcie czeka na „Rozpoznaj",
        # bo dokładasz do niego ujęcie spodu (#66); lista wpisana palcem jest
        # kompletna w chwili wysłania, a jej wiersz nie ma przycisku zlecenia.
        item = await app.state.queue.request_recognition(item.id)
        logger.info("Intake: dyktowanie #%s (%s znaków)", item.id, len(text))
        return item.as_dict()

    @app.get("/api/intake", tags=["intake"])
    async def list_intake(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        items = await app.state.queue.list(status=status, limit=min(limit, 200))

        def dump(item: Any) -> dict[str, Any]:
            data = item.as_dict()
            # Link do rekordu Part-DB budujemy tutaj, bo tylko warstwa web zna
            # publiczny adres instancji (#69). Kolejka oddaje sam numer części.
            for record in data.get("created_parts", []):
                if record.get("part_id") is not None:
                    record["url"] = settings.partdb_part_url(record["part_id"])
            return data

        # Ile propozycji czeka na decyzję przy każdej pozycji. Po tym PWA
        # rozróżnia „czeka na rozpoznanie" od „czeka na Twoje OK" — bez
        # dokładania stanu do kolejki (ADR-021).
        pending = await app.state.proposals.list(status="pending", limit=500)
        per_item: dict[int, int] = {}
        for proposal in pending:
            per_item[proposal.item_id] = per_item.get(proposal.item_id, 0) + 1

        payload = []
        for item in items:
            data = dump(item)
            data["proposals_pending"] = per_item.get(item.id, 0)
            payload.append(data)

        counts = await app.state.queue.counts()
        counts["awaiting_review"] = len(per_item)
        counts["proposals_pending"] = len(pending)
        return {"items": payload, "counts": counts}

    @app.get("/api/intake/{item_id}/photo", tags=["intake"])
    async def intake_photo(
        item_id: int, crop: str | None = None, w: int | None = None
    ) -> Response:
        """Podgląd zdjęcia — PWA pokazuje miniaturę tego, co przed chwilą
        wysłałeś, żeby dało się od razu zobaczyć, że ujęcie jest nieostre.

        `crop=x,y,w,h` (ułamki krawędzi, po obrocie z EXIF) oddaje **fragment**
        kadru. Tym adresem jedna płytka z grupowego zdjęcia trafia jako
        własne zdjęcie do własnej części w Part-DB (#72): wycinek nie jest
        osobnym plikiem, tylko czterema liczbami w adresie, więc oryginał
        zostaje jedynym plikiem do backupu, a zła ramka poprawia się
        podmianą adresu załącznika.
        """
        item = await app.state.queue.get(item_id)
        if item.kind != "photo" or not item.payload_path:
            raise HTTPException(status_code=404, detail="Ta pozycja nie ma zdjęcia")
        path = Path(item.payload_path)
        if not path.is_file():
            raise HTTPException(
                status_code=410, detail="Plik zdjęcia zniknął z dysku serwera"
            )

        if crop is None and w is None:
            media_type = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            return FileResponse(path, media_type=media_type)

        # `w` to dłuższy bok w pikselach — miniatura w liście Warsztatu
        # (ADR-022). Tylko kilka rozmiarów, żeby adres nie stał się
        # generatorem dowolnych obrazów na zamówienie.
        if w is not None and w not in PHOTO_EDGES:
            raise HTTPException(
                status_code=400,
                detail=f"w={w} — dozwolone: {', '.join(map(str, sorted(PHOTO_EDGES)))}",
            )
        box = None
        if crop is not None:
            try:
                box = CropBox.parse(crop)
            except CropError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Wycinek liczymy przy każdym żądaniu, ale adres jest niezmienny
        # (te same cztery liczby dają ten sam obraz), więc niech go trzyma
        # przeglądarka i Part-DB, a nie nasz dysk.
        headers = {"Cache-Control": "public, max-age=86400"}
        if w is None:
            fragment = prepare_for_agent(path, crop=box)
            return Response(
                content=fragment.data, media_type="image/jpeg", headers=headers
            )

        # Miniatury trzymamy na dysku. Zmniejszenie oryginału z telefonu
        # (4080 px, kilka MB) trwa kilkaset milisekund, a lista kategorii
        # w Warsztacie prosi o kilkadziesiąt naraz. Plik pochodny można
        # skasować w każdej chwili — odtworzy się z oryginału — dlatego leży
        # obok katalogu zdjęć, poza nocną kopią (`deploy/backup.sh`).
        key = f"{item_id}-{w}-{box.as_param() if box else 'full'}".replace(",", "_")
        cached = app.state.queue.photo_dir.parent / "thumbs" / f"{key}.jpg"
        if not cached.is_file():
            fragment = prepare_for_agent(path, crop=box, max_edge=w)
            cached.parent.mkdir(parents=True, exist_ok=True)
            partial = cached.with_suffix(".tmp")
            partial.write_bytes(fragment.data)
            partial.replace(cached)
        return FileResponse(cached, media_type="image/jpeg", headers=headers)

    @app.post("/api/intake/{item_id}/reject", tags=["intake"])
    async def reject_intake(item_id: int, note: str = Form(...)) -> dict[str, Any]:
        """Odrzucenie z powodem. Plik zostaje na dysku — przy nieudanym
        ujęciu chcesz zobaczyć, co poszło nie tak, zanim zrobisz drugie."""
        item = await app.state.queue.reject(item_id, note)
        return item.as_dict()

    @app.delete("/api/intake/{item_id}", tags=["intake"])
    async def delete_intake(item_id: int) -> dict[str, Any]:
        """Skasowanie pozycji razem ze zdjęciem — dla ujęć, które nie
        powinny były powstać: palec przed obiektywem, przypadkowy plik
        z galerii, podwójna wysyłka.

        Zatwierdzonych pozycji nie usuwamy: wskazują na części zapisane
        w Part-DB i są jedynym śladem pozwalającym ten zapis cofnąć.
        """
        item = await app.state.queue.delete(item_id)
        logger.info("Intake: usunięto pozycję #%s (%s)", item.id, item.kind)
        return {"deleted": item.id, "kind": item.kind}

    # --- propozycje do zatwierdzenia (ADR-021) ------------------------------

    def reviewer() -> ProposalReviewer:
        """Zatwierdzanie idzie przez ten sam `IntakeCommitter`, co w czacie."""
        return ProposalReviewer(
            app.state.proposals,
            app.state.queue,
            IntakeCommitter(app.state.partdb),
            settings.intake_photo_url,
        )

    @app.post("/api/intake/{item_id}/note", tags=["intake"])
    async def set_photo_note(
        item_id: int, text: str = Form(default="")
    ) -> dict[str, Any]:
        """Notatka do zdjęcia: ile sztuk, co to jest, czego nie widać (#75).

        Agent czyta ją razem z obrazem. Pusty tekst kasuje notatkę.
        """
        item = await app.state.queue.set_photo_note(item_id, text)
        logger.info("Intake: notatka do zdjęcia #%s (%s znaków)", item.id, len(text))
        return item.as_dict()

    @app.post("/api/intake/{item_id}/location", tags=["intake"])
    async def set_location_hint(
        item_id: int, text: str = Form(default="")
    ) -> dict[str, Any]:
        """Pudełko, do którego rzecz trafi po zatwierdzeniu (#77).

        Podpowiedź, nie przypisanie — zapisuje się dopiero z częścią.
        Pusty tekst kasuje wskazanie.
        """
        item = await app.state.queue.set_location_hint(item_id, text)
        logger.info("Intake: pudełko dla #%s → %s", item.id, item.location_hint or "brak")
        return item.as_dict()

    @app.post("/api/intake/{item_id}/recognize", tags=["intake"])
    async def request_recognition(item_id: int, wanted: bool = True) -> dict[str, Any]:
        """Zleć rozpoznanie tej pozycji — albo zdejmij zlecenie.

        To jawne zlecenie, nie zgoda na przemiał kolejki: automat (#75)
        bierze wyłącznie oznaczone pozycje, bo zdjęcie nieoznaczone bywa
        niekompletne — dokładasz do niego ujęcie spodu (#66).
        """
        item = await app.state.queue.request_recognition(item_id, wanted=wanted)
        logger.info(
            "Intake: pozycja #%s %s do rozpoznania",
            item.id,
            "zlecona" if wanted else "zdjęta ze zleceń",
        )
        return item.as_dict()

    @app.get("/api/proposals", tags=["propozycje"])
    async def list_proposals(
        status: str = "pending", item_id: int | None = None, limit: int = 200
    ) -> dict[str, Any]:
        """Propozycje do odklikania na telefonie, pogrupowane po pozycji.

        Miniatura każdej karty to **wycinek** z tą jedną płytką (#72) —
        adres jest w propozycji, policzony przy rozpoznaniu.
        """
        proposals = await app.state.proposals.list(
            item_id=item_id,
            status=None if status == "all" else status,
            limit=min(limit, 500),
        )
        return {
            "proposals": [proposal.as_dict() for proposal in proposals],
            "counts": await app.state.proposals.counts(),
        }

    @app.patch("/api/proposals/{proposal_id}", tags=["propozycje"])
    async def edit_proposal(
        proposal_id: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Popraw kartę z telefonu: nazwa, ilość, kategoria, lokalizacja, notatka.

        Pól rozpoznania (wycinek, numer ujęcia, rozstrzygnięcia dedupu) nie
        ruszamy palcem — ich zmiana znaczyłaby, że całe rozpoznanie jest do
        wyrzucenia, a nie do poprawki.
        """
        proposal = await app.state.proposals.edit(proposal_id, changes)
        return proposal.as_dict()

    @app.post("/api/proposals/{proposal_id}/approve", tags=["propozycje"])
    async def approve_proposal(proposal_id: int) -> dict[str, Any]:
        """✓ — dopiero to rusza magazyn."""
        try:
            proposal, refs = await reviewer().approve(proposal_id)
        except PartDbError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Part-DB odrzucił zapis: {exc}",
            ) from exc
        logger.info("Propozycja #%s zatwierdzona: %s", proposal_id, refs)
        return {"proposal": proposal.as_dict(), "created": refs}

    @app.post("/api/proposals/{proposal_id}/reject", tags=["propozycje"])
    async def reject_proposal(
        proposal_id: int, note: str = Form(default="")
    ) -> dict[str, Any]:
        """✕ — propozycja odpada, zdjęcie zostaje w kolejce."""
        proposal = await reviewer().reject(proposal_id, note)
        return proposal.as_dict()

    @app.post("/api/intake/{item_id}/approve", tags=["propozycje"])
    async def approve_item(item_id: int) -> dict[str, Any]:
        """Zatwierdź wszystkie propozycje pozycji, jedna po drugiej.

        Pojedynczo, nie hurtem: deduplikacja każdej kolejnej części widzi to,
        co powstało chwilę wcześniej. Przy zdjęciu grupowym (#72) sąsiednie
        płytki bywają podobne, a plan policzony raz na początku nie
        wiedziałby o pierwszej z nich.
        """
        try:
            approved, refs = await reviewer().approve_item(item_id)
        except PartDbError as exc:
            raise HTTPException(
                status_code=502, detail=f"Part-DB odrzucił zapis: {exc}"
            ) from exc
        return {
            "approved": [proposal.as_dict() for proposal in approved],
            "created": refs,
        }

    # --- Warsztat: przeglądanie magazynu (ADR-022) --------------------------

    @app.get("/api/warsztat/snapshot", tags=["warsztat"])
    async def warsztat_snapshot_route() -> dict[str, Any]:
        """Cały magazyn w postaci wierszy listy — szukanie dzieje się w
        przeglądarce, więc nie ma tu parametru `q`."""
        try:
            return await warsztat_snapshot(app.state.partdb, settings.public_base_url)
        except PartDbError as exc:
            raise HTTPException(
                status_code=502, detail=f"Part-DB nie odpowiedział: {exc}"
            ) from exc

    @app.get("/api/warsztat/parts/{part_id}", tags=["warsztat"])
    async def warsztat_part(part_id: int) -> dict[str, Any]:
        """Karta części: opis, uwagi, zdjęcia, datasheet, sklep, parametry."""
        try:
            return await fresh_card(part_id)
        except PartDbError as exc:
            status = 404 if exc.status == 404 else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    # --- Warsztat: zapisy z karty części (ADR-022, etap 2) -----------------
    #
    # Każdy zapis oddaje świeżą kartę razem z wierszem listy (`row`), więc
    # telefon podmienia jedną część zamiast pobierać cały magazyn.

    async def fresh_card(part_id: int) -> dict[str, Any]:
        card = await part_card(app.state.partdb, part_id, settings.public_base_url)
        # Link "Part-DB" na karcie: adres instancji zna tylko konfiguracja.
        card["partdb_url"] = settings.partdb_part_url(part_id)
        return card

    async def run_edit(part_id: int, action: Any) -> dict[str, Any]:
        try:
            await action
        except edit.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PartDbError as exc:
            raise HTTPException(
                status_code=502, detail=f"Part-DB odrzucił zapis: {exc}"
            ) from exc
        return await fresh_card(part_id)

    @app.post("/api/warsztat/parts/{part_id}", tags=["warsztat"])
    async def warsztat_edit_part(
        part_id: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Nazwa, opis, uwagi, kategoria, sprawdzone, dane techniczne, linki."""
        return await run_edit(
            part_id, edit.update_fields(app.state.partdb, part_id, changes)
        )

    @app.post("/api/warsztat/parts/{part_id}/quantity", tags=["warsztat"])
    async def warsztat_quantity(part_id: int, body: dict[str, Any]) -> dict[str, Any]:
        n = body.get("n")
        return await run_edit(
            part_id,
            edit.set_quantity(
                app.state.partdb,
                part_id,
                str(body.get("kind") or ""),
                None if n is None else float(n),
                bool(body.get("low")),
            ),
        )

    @app.post("/api/warsztat/parts/{part_id}/take", tags=["warsztat"])
    async def warsztat_take(part_id: int) -> dict[str, Any]:
        """„−1 wziąłem”."""
        return await run_edit(part_id, edit.take_one(app.state.partdb, part_id))

    @app.post("/api/warsztat/parts/{part_id}/box", tags=["warsztat"])
    async def warsztat_assign(part_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """Włóż do pudełka; z `from` — przenieś z tamtego."""
        source = body.get("from")
        return await run_edit(
            part_id,
            edit.assign_box(
                app.state.partdb,
                part_id,
                int(body["box"]),
                None if source is None else int(source),
            ),
        )

    @app.delete("/api/warsztat/parts/{part_id}/box/{box_id}", tags=["warsztat"])
    async def warsztat_unassign(part_id: int, box_id: int) -> dict[str, Any]:
        return await run_edit(
            part_id, edit.remove_from_box(app.state.partdb, part_id, box_id)
        )

    @app.post("/api/warsztat/parts/{part_id}/photos", tags=["warsztat"])
    async def warsztat_add_photo(part_id: int, photo: UploadFile) -> dict[str, Any]:
        """Zdjęcie części z aparatu. Plik leży u nas (w katalogu objętym nocną
        kopią), a w Part-DB jest jego adres — jak przy zdjęciach z intake'u."""
        media_type = resolve_image_type(photo.content_type or "", photo.filename or "")
        if media_type is None:
            raise HTTPException(status_code=400, detail="To nie jest zdjęcie")
        data = await photo.read()
        if not data:
            raise HTTPException(status_code=400, detail="Pusty plik")
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="Zdjęcie większe niż 25 MB")
        folder = app.state.queue.photo_dir / "parts"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        name = f"{part_id}-{stamp}{ALLOWED_IMAGE_TYPES[media_type]}"
        (folder / name).write_bytes(data)
        url = f"{settings.public_base_url}/api/warsztat/photo/{name}"
        return await run_edit(
            part_id,
            edit.add_photo(
                app.state.partdb, part_id, url, f"Zdjęcie własne ({stamp[:8]})"
            ),
        )

    @app.get("/api/warsztat/photo/{name}", tags=["warsztat"])
    async def warsztat_photo(name: str, w: int | None = None) -> Response:
        if "/" in name or ".." in name:
            raise HTTPException(status_code=400, detail="Zła nazwa pliku")
        path = app.state.queue.photo_dir / "parts" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Nie ma takiego zdjęcia")
        if w is None:
            return FileResponse(path)
        if w not in PHOTO_EDGES:
            raise HTTPException(status_code=400, detail=f"w={w} — niedozwolony rozmiar")
        cached = app.state.queue.photo_dir.parent / "thumbs" / f"part-{name}-{w}.jpg"
        if not cached.is_file():
            fragment = prepare_for_agent(path, max_edge=w)
            cached.parent.mkdir(parents=True, exist_ok=True)
            partial = cached.with_suffix(".tmp")
            partial.write_bytes(fragment.data)
            partial.replace(cached)
        return FileResponse(
            cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post(
        "/api/warsztat/parts/{part_id}/photos/{attachment_id}/main", tags=["warsztat"]
    )
    async def warsztat_main_photo(part_id: int, attachment_id: int) -> dict[str, Any]:
        return await run_edit(
            part_id, edit.set_main_photo(app.state.partdb, part_id, attachment_id)
        )

    @app.delete(
        "/api/warsztat/parts/{part_id}/photos/{attachment_id}", tags=["warsztat"]
    )
    async def warsztat_remove_photo(part_id: int, attachment_id: int) -> dict[str, Any]:
        return await run_edit(
            part_id, edit.remove_photo(app.state.partdb, part_id, attachment_id)
        )

    @app.post("/api/warsztat/parts/{part_id}/delete", tags=["warsztat"])
    async def warsztat_delete(part_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """Nieodwracalne — wymaga przepisania nazwy części."""
        try:
            await edit.delete_part(
                app.state.partdb, part_id, str(body.get("confirm") or "")
            )
        except edit.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": part_id}

    @app.get("/api/warsztat/boxes/next", tags=["warsztat"])
    async def warsztat_next_box(parent: int | None = None) -> dict[str, Any]:
        """Podpowiedź nazwy i kodu dla nowego pudełka."""
        locations = await app.state.partdb.list_storage_locations()
        try:
            name, code = edit.next_box_name(locations, parent)
        except edit.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"name": name, "code": code}

    @app.post("/api/warsztat/boxes", tags=["warsztat"])
    async def warsztat_create_box(body: dict[str, Any]) -> dict[str, Any]:
        """Nowe pudełko — zakłada je człowiek przy regale (ADR-022)."""
        parent = body.get("parent")
        try:
            created = await edit.create_box(
                app.state.partdb,
                None if parent is None else int(parent),
                str(body.get("name") or ""),
            )
        except edit.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        boxes = build_boxes(
            await app.state.partdb.list_storage_locations(), settings.public_base_url
        )
        return {"created": created.get("id"), "boxes": boxes}

    @app.get("/api/warsztat/media", tags=["warsztat"])
    async def warsztat_media(path: str) -> Response:
        """Plik trzymany w Part-DB (`/media/…`) — bez tokenu po stronie telefonu."""
        try:
            data, media_type = await app.state.partdb.fetch_media(path)
        except PartDbError as exc:
            status = 404 if exc.status == 404 else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # --- PWA ----------------------------------------------------------------
    #
    # Strona startowa to Warsztat (ADR-022, etap 4), a intake jest jego
    # zakładką „Dodaj” pod `/dodaj`. Pliki intake'u zostają w korzeniu
    # `pwa/` — service worker, manifest i cel udostępniania muszą leżeć
    # w zasięgu `/`, a przenosiny złamałyby zainstalowane aplikacje.
    no_cache = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        return FileResponse(PWA_DIR / "warsztat" / "index.html", headers=no_cache)

    @app.get("/dodaj", include_in_schema=False)
    async def add_page() -> RedirectResponse:
        # „Dodaj” jest zakładką Warsztatu (projekt v2). Stary ekran intake'u
        # zostaje pod /index.html na wypadek, gdyby nowy czegoś nie umiał.
        return RedirectResponse("/#dodaj", status_code=302)

    @app.get("/warsztat/", include_in_schema=False)
    async def old_warsztat_address() -> RedirectResponse:
        # Adres z etapu 1 mógł trafić do zakładek w telefonie.
        return RedirectResponse("/", status_code=301)

    # Statyczne pliki na końcu, żeby nie przykryły tras API.
    if PWA_DIR.is_dir():
        app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")
    else:  # pragma: no cover — tylko przy niekompletnym obrazie
        logger.warning("Brak katalogu PWA (%s) — serwuję samo API", PWA_DIR)

    return app


app_factory = create_app
