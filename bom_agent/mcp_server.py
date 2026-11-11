"""Serwer MCP `bom-agent` — narzędzia, których wbudowany MCP Part-DB nie ma.

Podział ról jest z ADR-016: odczyt surowych danych obsługuje serwer wbudowany
w Part-DB (21 narzędzi), a tutaj mieszka wnioskowanie, zapis i intake.

Transport: stdio. Wariant HTTP z tokenem to osobne zadanie (#15).

> **`FastMCP` nazywa się teraz `MCPServer`.** ADR-003 i ADR-016 mówią
> „FastMCP", bo tak nazywała się ta warstwa w SDK 1.x. W oficjalnym SDK 2.0
> klasa nosi nazwę `MCPServer` (`mcp.server.mcpserver`) — to ten sam,
> dekoratorowy sposób pisania serwera, nie inna biblioteka. Trzymamy się
> aktualnego API zamiast przypinać porzuconą wersję SDK.

Uruchomienie:

    uv run python -m bom_agent.mcp_server
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import __version__
from .assign import AssignError, LocationAssigner
from .config import Settings, load_settings
from .db import Database
from .digest import inventory_digest as build_inventory_digest
from .enrich import EnrichError, PartEnricher
from .intake.commit import CommitError, IntakeCommitter, PartSpec, SourcePhoto
from .intake.images import CropBox, CropError, prepare_for_agent
from .intake.proposals import ProposalError, ProposalStore
from .intake.queue import IntakeError, IntakeQueue
from .locations import location_label
from .partdb_client import PartDbClient, PartDbError
from .sources import SourceError, SourceRecorder

mcp = MCPServer(
    name="bom-agent",
    version=__version__,
    instructions=(
        "Magazyn części elektronicznych warsztatu. Zanim zaproponujesz układ "
        "albo listę zakupów, wołaj inventory_digest() — projekt ma powstawać "
        "na częściach, które są na stanie. Odczyt szczegółów części, "
        "wyszukiwanie u dostawców i dane katalogowe obsługuje osobny serwer "
        "MCP wbudowany w Part-DB.\n\n"
        "Rola użytkownika kończy się na zdjęciu wrzuconym do kolejki — "
        "resztę dobierasz Ty. Po każdym intake_commit() spróbuj uzupełnić "
        "nową część: enrich_part() dla rzeczy z katalogów TME i LCSC, "
        "record_source() dla modułów ze sklepów (Botland, Allegro), gdzie "
        "dane czytasz ze strony produktu. Nie pytaj użytkownika o numery "
        "katalogowe ani linki — poszukaj ich sam i pokaż plan do akceptacji."
    ),
)

_settings: Settings | None = None
_client: PartDbClient | None = None
_queue: IntakeQueue | None = None
_proposals: ProposalStore | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def get_queue() -> IntakeQueue:
    """Kolejka intake — ta sama baza, do której pisze PWA."""
    global _queue
    if _queue is None:
        settings = get_settings()
        database = Database(settings.bom_db_path)
        database.migrate()
        _queue = IntakeQueue(database, settings.intake_photo_dir)
    return _queue


def get_proposals() -> ProposalStore:
    """Magazyn propozycji — ta sama baza, z której czyta PWA (ADR-021).

    Własne połączenie, nie pożyczone z kolejki: `Database` otwiera plik na
    operację, więc dwa obiekty nad tym samym plikiem nic nie kosztują, a
    narzędzia dają się testować osobno.
    """
    global _proposals
    if _proposals is None:
        database = Database(get_settings().bom_db_path)
        database.migrate()
        _proposals = ProposalStore(database)
    return _proposals


def get_client() -> PartDbClient:
    """Leniwy singleton klienta.

    Tworzenie go dopiero przy pierwszym wywołaniu narzędzia sprawia, że
    serwer wstaje i daje się wylistować nawet przy złej konfiguracji —
    a błąd trafia do odpowiedzi narzędzia, gdzie da się go przeczytać,
    zamiast ubić proces na starcie.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = PartDbClient(
            settings.partdb_url,
            settings.partdb_api_token,
            timeout=settings.partdb_timeout,
        )
    return _client


@mcp.tool()
async def inventory_digest(scope: str | None = None) -> str:
    """Zwięzły zrzut całego magazynu: co leży w kartonach, ile i gdzie.

    Wołaj to na starcie sesji projektowej, zanim zaproponujesz jakikolwiek
    układ — projekt ma powstawać na częściach, które są na stanie.

    Sekcja „ZNANE, ALE NIE NA STANIE" to wpisy katalogowe bez lokalizacji.
    Ich NIE MA w kartonie, mimo że baza je zna. Ilości z `~` są przybliżone
    (klasa B: rezystory, kondensatory), a `brak` oznacza pozycję, która
    w magazynie jest, ale się skończyła.

    Args:
        scope: opcjonalne zawężenie — fragment nazwy części, kategorii
            albo kodu lokalizacji, np. "czujniki" lub "A/A1".
    """
    try:
        return await build_inventory_digest(get_client(), scope=scope)
    except PartDbError as exc:
        return (
            f"Nie udało się pobrać stanu magazynu z Part-DB: {exc}\n\n"
            "Zrzut jest niedostępny — nie zgaduj zawartości magazynu "
            "i powiedz użytkownikowi, że baza nie odpowiada."
        )


# `structured_output=False` jest konieczne: narzędzie zwraca mieszankę tekstu
# i obrazów, a przy włączonym wyniku strukturalnym SDK próbuje zserializować
# obiekt `Image` do JSON-a i przerywa wywołanie.
@mcp.tool(structured_output=False)
async def intake_pending(limit: int = 5, only_requested: bool = False) -> list[Any]:
    """Pokaż oczekujące pozycje z kolejki intake — zdjęcia jako obrazy.

    To Ty rozpoznajesz części na zdjęciach; nie ma tu żadnej usługi
    wizyjnej (ADR-005). Dla każdej pozycji dostajesz nagłówek z numerem
    i podpowiedzią lokalizacji, a potem jej zdjęcia.

    **Pozycja może mieć kilka ujęć** — moduł sfotografowany z wierzchu,
    od spodu i z bliska na sitodruk. To jeden element opisany kilkoma
    obrazami, więc rozpoznajesz go raz i raz zatwierdzasz; potraktowanie
    ujęć jako osobnych części dołożyłoby do magazynu sztuki, których nie ma.

    **Jedno ujęcie może za to pokazywać kilka RÓŻNYCH płytek** — tak wygląda
    zdjęcie wysypanej na blat paczki. To odwrotna sytuacja: tyle części, ile
    modułów, i każda z własnym obrazem. Podaj wtedy w `intake_commit()`
    przy każdej części jej `crop` — ramkę wokół jej płytki, w ułamkach
    krawędzi kadru (`[x, y, szerokość, wysokość]`, `0,0` = lewy górny róg,
    orientacja ta, w której widzisz zdjęcie tutaj). Bez tego wszystkie
    części dostają ten sam kadr z obcymi modułami i na liście w magazynie
    nie da się poznać, która jest która.

    Ramkę **sprawdź przed zapisem** przez `intake_zoom()` — zobaczysz sam
    wycinek, powiększony. To ta sama operacja, która pozwala doczytać
    nieczytelny z daleka sitodruk: wycinek idzie z oryginału, nie ze
    zmniejszonej kopii, więc ćwiartka kadru ma pełne 1000 px szczegółu.

    Zasady rozpoznawania, bez wyjątków:
    * **Nie zgaduj oznaczeń.** Fałszywa nazwa części trafia do magazynu
      i ktoś projektuje na niej układ. „Nieczytelne" jest poprawną
      odpowiedzią, wymyślone `ESP32-WROOM-32` nie jest.
    * Gdy typ modułu wynika z topologii, ale nadruk jest nieczytelny —
      zapisz go z `needs_review=true` i napisz w `note`, skąd wniosek.
    * Ilość liczysz z tego, co widać. Przy częściach biernych używaj
      `instock_unknown=true` zamiast udawać dokładność (ADR-007).
    * Przy niepewności **pytaj użytkownika**, zamiast zapisywać po cichu.

    **Pozycje dyktowane** (`kind` = `voice`) mają treść zamiast zdjęcia.
    Rozbicie zdania na części jest Twoje — to wymaga wiedzy o elektronice,
    nie wyrażeń regularnych (ADR-006). Przykład:

        „kuweta B3, dużo 100nF 0805, ze dwadzieścia elektrolitów 470u"

        → {"name": "Kondensator 100nF 0805", "bucket": "dużo"}
          {"name": "Kondensator elektrolityczny 470uF", "amount": 20,
           "instock_unknown": true}

    Reguły, których dyktowanie nie może naruszyć:
    * **Ilość mglistą podawaj jako `bucket`** (`mało` / `sporo` / `dużo` /
      `brak`), a nie zmyśloną liczbą. Przelicznik jest wspólny dla całego
      systemu, więc „dużo" znaczy to samo w każdej sesji.
    * **Separator dziesiętny zostaje**: `1.2pF` to nie `12pF`.
    * Dyktowanie myli oznaczenia (`ESP32 WROOM` bywa słyszane jako
      `ST32 VRO`). Gdy człon nie składa się w znany układ — **dopytaj**,
      zamiast zapisywać część, która nie istnieje.

    Rozpoznane pozycje zatwierdzasz przez `intake_commit()`.

    Args:
        limit: ile pozycji pokazać (domyślnie 5). Każde zdjęcie to około
            2,5 tys. tokenów, więc nie bierz całego kartonu naraz.
        only_requested: `true` = wyłącznie pozycje, przy których użytkownik
            wcisnął „Rozpoznaj" na telefonie, najdłużej czekające pierwsze.
            Tak pracuje automat (#75): zlecenie jest jawne, a zdjęcie
            nieoznaczone bywa niekompletne — użytkownik zamierza dołożyć
            ujęcie spodu (#66).
    """
    queue = get_queue()
    store = get_proposals()
    # Sesja bez nadzoru (#75) startuje z INTAKE_ONLY_REQUESTED=1 i wtedy
    # zawężenie przestaje być prośbą w argumencie, a staje się właściwością
    # narzędzia: nieoznaczone zdjęcie nie istnieje dla tej sesji, choćby
    # model poprosił o całą kolejkę. Zdanie w promptcie nie jest gwarancją.
    locked = get_settings().intake_only_requested
    only_requested = only_requested or locked
    try:
        window = max(1, min(limit, 20))
        items = (
            await queue.requested(limit=window)
            if only_requested
            else await queue.list(status="pending", limit=window)
        )
    except IntakeError as exc:
        return [f"Nie udało się odczytać kolejki: {exc}"]

    # Pozycja z propozycjami czeka na człowieka, nie na rozpoznanie.
    # Pokazanie jej tutaj prosiłoby o drugie rozpoznanie tej samej rzeczy
    # i skończyło się dwoma kompletami propozycji do odklikania (ADR-021).
    awaiting = await store.items_with_pending()
    skipped = [item.id for item in items if item.id in awaiting]
    items = [item for item in items if item.id not in awaiting]

    if not items:
        if skipped:
            return [
                f"Nie ma nic do rozpoznania. Pozycje {skipped} mają już "
                "propozycje i czekają na zatwierdzenie przez użytkownika."
            ]
        return [
            "Nie ma zleconych pozycji do rozpoznania."
            if only_requested
            else "Kolejka intake jest pusta — nie ma nic do rozpoznania."
        ]

    counts = await queue.counts()
    header = (
        f"Zleconych do rozpoznania: {len(items)}."
        if only_requested
        else f"Oczekujących pozycji: {counts['pending']}, pokazuję {len(items)}."
    )
    if skipped:
        header += f" Pominięte (mają już propozycje do zatwierdzenia): {skipped}."
    # Taksonomia magazynu w nagłówku. Bez niej sesja bez nadzoru wymyśla
    # kategorie od zera („moduł GPS", „antena GPS") zamiast użyć istniejących
    # — zmierzone na pierwszym przebiegu automatu. Lista jest tania, a
    # zatwierdzenie takiej propozycji zakłada w bazie śmieciową kategorię,
    # której nikt potem nie scali.
    try:
        known = sorted(
            (category.get("full_path") or category.get("name") or "").strip()
            for category in await get_client().list_categories()
        )
        known = [name for name in known if name]
        if known:
            header += (
                "\nKategorie w bazie (użyj istniejącej, jeśli pasuje; nowa "
                f"tylko wtedy, gdy naprawdę nic nie pasuje): {', '.join(known)}."
            )
    except PartDbError:
        # Brak taksonomii nie może zatrzymać rozpoznawania — plan i tak
        # pokaże, że kategoria jest nowa.
        header += "\nUWAGA: nie udało się odczytać listy kategorii z Part-DB."

    if locked:
        header += (
            "\nTa sesja widzi WYŁĄCZNIE pozycje zlecone przyciskiem w aplikacji "
            "(INTAKE_ONLY_REQUESTED=1) — reszta kolejki jest dla niej "
            "niewidoczna i tak ma być."
        )
    blocks: list[Any] = [header]

    for item in items:
        header = [f"── pozycja #{item.id} ({item.kind}), dodana {item.created_at}"]
        header.append(
            f"   lokalizacja: {item.location_hint}"
            if item.location_hint
            else "   lokalizacja: BRAK — dopytaj użytkownika, zanim zatwierdzisz"
        )

        if item.kind != "photo":
            header.append(f"   treść: {item.payload_text}")
            blocks.append("\n".join(header))
            continue

        # Użytkownik trzyma tę rzecz w ręce: liczba sztuk i nazwa z notatki
        # są pewniejsze niż to, co da się wyczytać ze zdjęcia (#75).
        if item.payload_text:
            header.append(f"   notatka użytkownika: {item.payload_text}")

        # Kilka ujęć to nadal JEDNA rzecz (#66) — sfotografowana z wierzchu,
        # od spodu i z bliska na sitodruk. Pokazujemy je razem, bo dopiero
        # razem opisują ten sam moduł.
        shots = [Path(shot.payload_path) for shot in item.frames]
        if len(shots) > 1:
            header[0] += f" — {len(shots)} ujęcia tej samej rzeczy"

        prepared = []
        for index, path in enumerate(shots, start=1):
            if not path.is_file():
                header.append(f"   UWAGA: plik ujęcia {index} zniknął z dysku serwera")
                continue
            try:
                image = prepare_for_agent(path)
            except OSError as exc:
                # Zmierzone na instancji: jeden uszkodzony plik (ucięta
                # wysyłka, plik z testu) wywracał CAŁE narzędzie i kolejka
                # stawała się nieczytelna — także dla automatu. Jedno złe
                # ujęcie ma być jedną linijką ostrzeżenia, nie awarią.
                header.append(
                    f"   UWAGA: ujęcia {index} nie da się odczytać ({exc}) — "
                    "poproś użytkownika o powtórzenie zdjęcia"
                )
                continue
            header.append(
                f"   ujęcie {index}: {image.source_width}×{image.source_height} "
                f"→ {image.width}×{image.height} ({image.size_kb} kB)"
            )
            prepared.append(image)

        blocks.append("\n".join(header))
        blocks += [Image(data=image.data, format="jpeg") for image in prepared]

    return blocks


# Jak `intake_pending`: zwraca tekst i obraz, więc wynik strukturalny musi
# być wyłączony — inaczej SDK próbuje zserializować `Image` do JSON-a.
@mcp.tool(structured_output=False)
async def intake_zoom(item_id: int, crop: str, shot: int = 1) -> list[Any]:
    """Pokaż powiększony fragment zdjęcia z kolejki.

    Dwa zastosowania, oba praktyczne:

    * **Doczytanie nadruku.** Wycinek robimy z oryginału (4080 px), nie ze
      zmniejszonego kadru, który widziałeś w `intake_pending()`. Ćwiartka
      zdjęcia to nadal tysiąc pikseli szczegółu, więc `AMS1117-3.3`
      nieczytelne w całym kadrze bywa czytelne tutaj. To jest właściwa
      reakcja na „nie widzę oznaczenia" — przed odrzuceniem pozycji.
    * **Sprawdzenie ramki przed zapisem.** Gdy na jednym ujęciu leży kilka
      płytek, każda część dostaje swój `crop` w `intake_commit()`. Zła ramka
      podpina do części zdjęcie sąsiedniego modułu, a tego nie widać po
      liczbach — widać po obrazie.

    Args:
        item_id: numer pozycji z `intake_pending()`.
        crop: ramka w ułamkach krawędzi kadru — `"x,y,szerokość,wysokość"`,
            gdzie `0,0` to lewy górny róg, a orientacja jest ta, w której
            widzisz zdjęcie w `intake_pending()`. Procenty (`"10,20,30,40"`)
            też są rozumiane. Przykład: prawa górna ćwiartka to
            `"0.5,0,0.5,0.5"`.
        shot: numer ujęcia pozycji, licząc od 1 (domyślnie zdjęcie główne).
    """
    queue = get_queue()
    try:
        item = await queue.get(item_id)
    except IntakeError as exc:
        return [f"Nie udało się odczytać pozycji: {exc}"]

    frames = item.frames
    if not frames:
        return [f"Pozycja #{item_id} nie ma zdjęcia — nie ma czego przybliżać."]
    if shot < 1 or shot > len(frames):
        return [
            f"Pozycja #{item_id} ma {len(frames)} ujęć, "
            f"a pytasz o {shot}. Ujęcia numerujemy od 1."
        ]

    try:
        box = CropBox.parse(crop)
    except CropError as exc:
        return [f"Zły wycinek: {exc}"]

    path = Path(frames[shot - 1].payload_path)
    if not path.is_file():
        return [f"Plik ujęcia {shot} pozycji #{item_id} zniknął z dysku serwera."]

    try:
        fragment = prepare_for_agent(path, crop=box)
    except OSError as exc:
        return [
            f"Ujęcia {shot} pozycji #{item_id} nie da się odczytać ({exc}) — "
            "plik jest uszkodzony albo ucięty."
        ]
    header = (
        f"── pozycja #{item_id}, ujęcie {shot}, wycinek {box.as_param()}\n"
        f"   {fragment.source_width}×{fragment.source_height} → "
        f"{fragment.width}×{fragment.height} ({fragment.size_kb} kB)"
    )
    return [header, Image(data=fragment.data, format="jpeg")]


@mcp.tool()
async def intake_commit(
    item_id: int,
    parts: list[dict],
    location: str = "",
    confirm: bool = False,
) -> str:
    """Zatwierdź rozpoznanie pozycji kolejki: zapisz części do Part-DB.

    **Bez `confirm=true` zwraca wyłącznie plan i niczego nie zapisuje.**
    Pokaż ten plan użytkownikowi i dopiero po jego akceptacji wołaj
    ponownie z `confirm=true`. To zabezpieczenie jest przewidziane, nie
    niedoróbka: pomyłka psuje kilkanaście pozycji naraz.

    Część istniejąca (trafienie po MPN albo znormalizowanej nazwie)
    dostaje **nowy stan w tej lokalizacji**, a nie duplikat części.

    Nowo utworzona część dostaje **wszystkie ujęcia** pozycji jako
    załączniki, a pierwsze z nich jako miniaturę — przy module z wytartym
    nadrukiem to jedyny obraz dokładnie tej sztuki, którą masz w ręce.
    Zatwierdzenie pozycji zamyka też jej pozostałe ujęcia, więc nie wrócą
    do kolejki jako osobne rzeczy.

    **Kilka płytek na jednym zdjęciu: podaj każdej części `crop`.** Wtedy
    dostaje sam swój wycinek, a nie kadr z czterema obcymi modułami —
    inaczej lista części w magazynie pokazuje pięć razy to samo zdjęcie
    i miniatura przestaje cokolwiek znaczyć. Ramkę sprawdź wcześniej przez
    `intake_zoom()`; plan pokazuje adresy wycinków, więc widać w nim, czy
    dwie części nie dostały przypadkiem tej samej.

    Args:
        item_id: numer pozycji z `intake_pending()`.
        parts: lista rozpoznanych części. Każda: `name` (wymagane),
            `amount`, `category`, `mpn`, `description`, `manufacturer`,
            `instock_unknown` (klasa B wg ADR-007), `needs_review`
            (rozpoznanie niepewne), `note`.
            Gdy dokładnej liczby nie znasz — zamiast `amount` podaj
            `bucket`: `mało` (<10), `sporo` (10–100), `dużo` (>100)
            albo `brak`. Kubełek sam włącza `instock_unknown`, bo liczba
            jest wtedy środkiem przedziału, a nie pomiarem.
            Przy niejednoznacznej deduplikacji dochodzą dwa pola
            rozstrzygające: `merge_into` z IRI istniejącej części
            („to ta sama rzecz") albo `force_new: true` („to co innego,
            mimo podobnej nazwy").
            Gdy na jednym ujęciu leży kilka różnych płytek, dochodzi
            `crop`: `[x, y, szerokość, wysokość]` w ułamkach krawędzi
            kadru (`0,0` = lewy górny róg, orientacja jak w
            `intake_pending()`; procenty też są rozumiane) oraz `shot` —
            numer ujęcia, z którego bierzesz wycinek, licząc od 1.
            Część z `crop` dostaje **tylko** ten wycinek.
        location: gdzie to leży — `A/A1`, `Kuweta A1` albo pełna ścieżka.
            Puste = użyj podpowiedzi z pozycji kolejki.
        confirm: `true` dopiero po akceptacji planu przez użytkownika.
    """
    queue = get_queue()
    committer = IntakeCommitter(get_client())

    try:
        item = await queue.get(item_id)
        if item.status in ("committed", "rejected"):
            return f"Pozycja #{item_id} jest już {item.status} — nie zapisuję ponownie."

        specs = [PartSpec.from_dict(raw) for raw in parts]
        if not specs:
            return "Pusta lista części — nie ma czego zapisać."

        # Zdjęcia z kolejki jadą razem z zapisem (#64): przy module bez
        # czytelnego nadruku to jedyny obraz tej konkretnej sztuki.
        # Ujęć może być kilka — wtedy część dostaje wszystkie (#66).
        photos = [
            SourcePhoto(url=get_settings().intake_photo_url(shot.id), item_id=shot.id)
            for shot in item.frames
        ]

        wanted = location.strip()
        hint = (item.location_hint or "").strip()
        try:
            plan = await committer.plan(specs, wanted or hint, photos=photos)
        except CommitError as exc:
            # Podpowiedź z telefonu to **plan użytkownika**, nie stan bazy:
            # przy dyktowaniu pada „kuweta B3", zanim taki pojemnik powstanie
            # (ADR-017). Rejestrujemy wtedy bez przypisania, zamiast odmawiać
            # zapisu — nieprzypisany stan jest właśnie tym, po czym poznaje
            # się rzeczy do rozłożenia. Lokalizacja podana wprost w wywołaniu
            # to co innego: tam błąd zostaje błędem.
            if wanted or not hint:
                raise
            plan = await committer.plan(specs, "", photos=photos)
            plan.warnings.append(
                f"Podpowiedź z telefonu („{hint}”) nie wskazuje na istniejący "
                f"pojemnik — zapisuję bez przypisania (ADR-017). {exc}"
            )

        if not confirm:
            return (
                f"PLAN dla pozycji #{item_id} — nic nie zapisano.\n\n{plan.as_text()}"
            )

        refs = await committer.execute(plan)
        await queue.commit(
            item_id,
            result=[
                {
                    "name": p.spec.name,
                    "amount": p.spec.amount,
                    "needs_review": p.spec.needs_review,
                }
                for p in plan.parts
            ],
            partdb_refs=refs,
        )
        summary = "\n".join(f"  · {p.describe()}" for p in plan.parts)
        warnings = (
            "\n\nUWAGI:\n" + "\n".join(f"  ! {w}" for w in plan.warnings)
            if plan.warnings
            else ""
        )
        return (
            f"Zapisane. Pozycja #{item_id} oznaczona jako committed.\n\n"
            f"{summary}\n\nUtworzone zasoby: {', '.join(refs)}{warnings}"
        )
    except (IntakeError, CommitError) as exc:
        return f"Nie zapisano: {exc}"
    except PartDbError as exc:
        return (
            f"Part-DB odrzucił zapis: {exc}\n\n"
            "Część pozycji mogła już powstać — sprawdź magazyn przed ponowieniem."
        )


@mcp.tool()
async def intake_propose(item_id: int, parts: list[dict], location: str = "") -> str:
    """Zapisz rozpoznanie jako **propozycje do zatwierdzenia na telefonie**.

    To druga droga zamknięcia pozycji, obok `intake_commit()` (ADR-021):

    * `intake_commit()` — rozmawiasz z użytkownikiem, pokazujesz plan,
      dostajesz „tak" i zapisujesz. Dwa moduły zamykasz w jednej wymianie
      zdań i tak jest szybciej.
    * `intake_propose()` — **niczego nie zapisujesz do magazynu**. Propozycje
      lądują w zakładce „Do zatwierdzenia" w aplikacji na telefonie, gdzie
      użytkownik klika ✓ przy każdej karcie. Tego używaj, gdy pracujesz bez
      użytkownika (rozpoznanie zlecone przyciskiem, #75) albo gdy pozycji
      jest tyle, że czytanie planu w czacie byłoby karą.

    Karta na telefonie pokazuje **wycinek** zdjęcia z tą jedną płytką (#72),
    nazwę, ilość, kategorię i werdykt deduplikacji. Werdykt zapisujemy tu,
    w chwili rozpoznania — dlatego **deduplikację rozstrzygasz Ty**, nie
    użytkownik: wybór między `merge_into=<IRI>` a `force_new` wymaga
    porównania dwóch rekordów w bazie, a ekran telefonu nie jest miejscem
    na takie pytanie. Nierozstrzygnięta niejednoznaczność blokuje zapis
    propozycji dokładnie tak, jak blokuje `intake_commit(confirm=true)`.

    Args:
        item_id: numer pozycji z `intake_pending()`.
        parts: to samo, co w `intake_commit()` — łącznie z `crop`, `shot`,
            `bucket`, `needs_review`, `merge_into` i `force_new`.
        location: gdzie to leży. Puste = podpowiedź z pozycji kolejki.
    """
    queue = get_queue()
    store = get_proposals()
    committer = IntakeCommitter(get_client())

    try:
        item = await queue.get(item_id)
        if item.status != "pending":
            return (
                f"Pozycja #{item_id} jest w stanie {item.status!r} — "
                "propozycje przyjmuje tylko pozycja czekająca."
            )

        specs = [PartSpec.from_dict(raw) for raw in parts]
        if not specs:
            return "Pusta lista części — nie ma czego proponować."

        existing = await store.list(item_id=item_id, status="pending")
        if existing:
            return (
                f"Pozycja #{item_id} ma już {len(existing)} propozycji "
                "czekających na zatwierdzenie — nie dokładam drugiego "
                "kompletu. Użytkownik ma je odklikać albo odrzucić."
            )

        wanted = location.strip()
        hint = (item.location_hint or "").strip()
        photos = [
            SourcePhoto(url=get_settings().intake_photo_url(shot.id), item_id=shot.id)
            for shot in item.frames
        ]
        try:
            plan = await committer.plan(specs, wanted or hint, photos=photos)
        except CommitError:
            # ADR-017: podpowiedź z telefonu to plan użytkownika, nie stan
            # bazy — rejestrujemy bez przypisania, zamiast odmawiać.
            if wanted or not hint:
                raise
            plan = await committer.plan(specs, "", photos=photos)

        if plan.undecided:
            names = ", ".join(part.spec.name for part in plan.undecided)
            return (
                f"Nie zapisuję propozycji: {names} ma w bazie części podobne, "
                "a nie takie same. Rozstrzygnij to teraz (merge_into=<IRI> "
                "albo force_new=true) — użytkownik nie zrobi tego z telefonu.\n\n"
                f"{plan.as_text()}"
            )

        # Adres wycinka jest **względny**: PWA i zdjęcia stoją pod tym samym
        # adresem, a wpisanie tu publicznego hosta zamroziłoby go w bazie.
        frames = item.frames
        entries = []
        for planned in plan.parts:
            spec = planned.spec
            shot = frames[min(spec.shot, len(frames)) - 1] if frames else None
            photo_url = None
            if shot is not None:
                photo_url = f"/api/intake/{shot.id}/photo"
                if spec.crop is not None:
                    photo_url += f"?crop={spec.crop.as_param()}"
            # Ładunek składamy z **przetworzonej** pozycji, nie z surowego
            # słownika od modelu. Karta na telefonie ma pokazywać to, co się
            # naprawdę stanie: kubełek jest już liczbą, brak ilości jest
            # jawną jedynką, a nie pustym polem, przy którym nie wiadomo,
            # czy zatwierdzenie zapisze jedną sztukę, czy zero.
            whole = spec.amount == int(spec.amount)
            amount = int(spec.amount) if whole else spec.amount
            payload: dict[str, Any] = {
                "name": spec.name,
                "amount": amount,
                "category": spec.category,
                "instock_unknown": spec.instock_unknown,
                "needs_review": spec.needs_review,
            }
            for key, value in (
                ("mpn", spec.mpn),
                ("manufacturer", spec.manufacturer),
                ("description", spec.description),
                ("note", spec.note),
                ("merge_into", spec.merge_into),
            ):
                if value:
                    payload[key] = value
            if spec.force_new:
                payload["force_new"] = True
            if spec.crop is not None:
                payload["crop"] = spec.crop.as_param()
                payload["shot"] = spec.shot
            if wanted or hint:
                payload["location"] = wanted or hint
            preview = {
                "photo_url": photo_url,
                "action": "merge" if planned.existing_part else "create",
                "new_category": planned.new_category,
                "location": (
                    plan.location.get("full_path") or plan.location.get("name")
                    if plan.location
                    else ""
                ),
            }
            if planned.existing_part:
                preview["existing"] = {
                    "name": planned.existing_part.get("name"),
                    "iri": planned.existing_part.get("@id"),
                }
            entries.append((payload, preview))

        created = await store.create(item_id, entries)
    except (IntakeError, CommitError, ProposalError) as exc:
        return f"Nie zapisano propozycji: {exc}"
    except PartDbError as exc:
        return (
            f"Part-DB nie odpowiedział przy sprawdzaniu duplikatów: {exc}\n\n"
            "Propozycji nie zapisałem — bez werdyktu deduplikacji karta na "
            "telefonie kłamałaby o tym, co się stanie po zatwierdzeniu."
        )

    lines = [
        f"  · {p.name}"
        + (
            f" → dołoży stan do {p.preview['existing']['name']}"
            if p.preview.get("action") == "merge"
            else " → utworzy część"
        )
        for p in created
    ]
    return (
        f"Zapisane jako propozycje ({len(created)}) — magazyn nietknięty.\n"
        + "\n".join(lines)
        + "\n\nUżytkownik zatwierdza je w aplikacji, w zakładce "
        "„Do zatwierdzenia”. Do tego czasu pozycja zostaje w kolejce."
    )


@mcp.tool()
async def intake_reject(item_id: int, note: str) -> str:
    """Odrzuć pozycję kolejki, podając powód.

    Zdjęcie zostaje na dysku — przy nieostrym ujęciu chcesz je obejrzeć,
    zanim poprosisz o powtórzenie.

    Args:
        item_id: numer pozycji.
        note: co jest nie tak, np. „rozmazane, nie widać sitodruku".
    """
    try:
        item = await get_queue().reject(item_id, note)
    except IntakeError as exc:
        return f"Nie odrzucono: {exc}"
    return f"Pozycja #{item.id} odrzucona: {item.note}"


@mcp.tool()
async def assign_location(
    location: str,
    parts: list[str] | None = None,
    category: str = "",
    confirm: bool = False,
) -> str:
    """Przypisz pojemnik rzeczom, które leżą w magazynie bez lokalizacji.

    Druga połowa ADR-017: intake rejestruje część **bez pudła**, bo przy
    spisywaniu kartonu struktura pojemników jeszcze nie istnieje. Tym
    narzędziem domyka się to później, przy sortowaniu: *biorę rzecz →
    baza mówi, do której kuwety trafia → wkładam i naklejam etykietę*.

    **Bez `confirm=true` zwraca sam plan i niczego nie zapisuje.**

    Trzy zasady, które to narzędzie egzekwuje:

    * **Rusza wyłącznie stany bez pojemnika.** To, co już gdzieś leży,
      zostaje pominięte z adnotacją — przeprowadzka to inna operacja (#38)
      i nie ma się zdarzać przy okazji.
    * **Pojemnika nie tworzy.** Nieznana nazwa kończy się błędem z listą
      istniejących. Pudło wymyślone w bazie to rzeczy „leżące" tam, gdzie
      nikt ich nie znajdzie.
    * **Niejednoznaczna nazwa wstrzymuje zapis.** `„kondensator 100n"`
      pasuje do wielu pozycji; wsadzenie nie tej części do kuwety jest
      gorsze niż niewsadzenie żadnej.

    Postęp sortowania widać w `inventory_digest()` — sekcja „MAM, ALE BEZ
    PRZYPISANIA" pustoszeje. Pusta znaczy: magazyn ułożony.

    Args:
        location: pojemnik — `A/A1`, `Kuweta A1` albo pełna ścieżka.
            Musi istnieć w Part-DB.
        parts: co przypisać — nazwy, numery katalogowe albo identyfikatory
            części z Part-DB (`parts/29` albo samo `29`). Alternatywa
            dla `category`.
        category: cała kategoria naraz (`Kondensatory SMD`) — przy
            sortowaniu to najczęstszy ruch. Alternatywa dla `parts`.
        confirm: `true` dopiero po akceptacji planu przez użytkownika.
    """
    assigner = LocationAssigner(get_client())
    try:
        plan = await assigner.plan(location, parts=parts, category=category)
        if not confirm:
            return f"PLAN — nic nie zapisano.\n\n{plan.as_text()}"

        changed = await assigner.execute(plan)
        summary = "\n".join(f"  · {lot.describe()}" for lot in plan.lots)
        skipped = plan.skipped_text()
        skipped = f"\n\n{skipped}" if skipped else ""
        return (
            f"Przypisane do: {location_label(plan.location)}\n\n{summary}\n\n"
            f"Zmienione stany: {', '.join(changed)}{skipped}"
        )
    except AssignError as exc:
        return f"Nie przypisano: {exc}"
    except PartDbError as exc:
        return (
            f"Part-DB odrzucił zmianę: {exc}\n\n"
            "Część stanów mogła już dostać pojemnik — sprawdź przed ponowieniem."
        )


@mcp.tool()
async def enrich_part(
    parts: list[str] | None = None,
    category: str = "",
    limit: int = 10,
    provider_key: str = "",
    provider_id: str = "",
    confirm: bool = False,
) -> str:
    """Dołóż częściom zdjęcie katalogowe, datasheet i parametry od dostawcy.

    Magazyn spisany ze zdjęć zna nazwę części i to, że leży w kartonie —
    ale nie zna jej dokumentacji. Przy projektowaniu układu rozstrzyga
    właśnie datasheet: czy stabilizator wyrobi 1 A i jaki ma pinout.

    **Bez `confirm=true` zwraca sam plan i niczego nie zapisuje.**

    Trzy zasady:

    * **Trafienie po numerze katalogowym bierzemy, po nazwie pytamy.**
      Zapytanie TME o `NE555` zwróciło jako pierwszy wynik płytkę
      MIKROE-2032 — automat dokleiłby do układu zdjęcie czegoś innego.
      Gdy pewnego trafienia nie ma, dostajesz kandydatów do rozstrzygnięcia.
    * **Plik zjeżdża do Part-DB**, nie zostaje linkiem do sklepu. Inaczej
      pierwsza przebudowa CDN-u dostawcy zostawia puste ramki, a kopia
      zapasowa nie ma czego objąć.
    * **Miniaturą zostaje Twoje własne zdjęcie**, jeśli część je ma;
      katalogowe pokazuje wzorzec, własne — tę sztukę z wytartym nadrukiem.

    Czego dostawcy nie dają (zmierzone na tej instancji): LCSC oddaje
    zdjęcia 900×900, TME **żadnych** poza miniaturą z wyszukiwania, za to
    więcej parametrów. Brak zdjęcia u dostawcy nie jest awarią.

    Args:
        parts: nazwy, numery katalogowe albo identyfikatory (`parts/29`).
        category: cała kategoria naraz. Alternatywa dla `parts`.
        limit: ile części w jednym wywołaniu (domyślnie 10). Dostawcy mają
            limity zapytań — ucięcie jest zgłaszane w planie, nie ciche.
        provider_key: gdy plan pokazał kandydatów i użytkownik wskazał
            właściwego — klucz dostawcy (`lcsc`, `tme`). Tylko dla jednej
            części naraz, razem z `provider_id`.
        provider_id: identyfikator rekordu u dostawcy (`C5125085`).
        confirm: `true` dopiero po akceptacji planu przez użytkownika.
    """
    enricher = PartEnricher(get_client())

    # Faza planu to czysty odczyt — od dostawcy i z Part-DB. Awarię tej fazy
    # nazywamy odczytem, nie zapisem: dawny komunikat „Part-DB odrzucił zapis"
    # kłamał przy błędzie wyszukiwarki dostawcy (#71).
    try:
        plan = await enricher.plan(
            parts=parts,
            category=category,
            limit=limit,
            provider_key=provider_key,
            provider_id=provider_id,
        )
    except EnrichError as exc:
        return f"Nie uzupełniono: {exc}"
    except PartDbError as exc:
        return f"Odczyt się nie powiódł: {exc}\n\nNic nie zapisano."

    if not confirm:
        return f"PLAN — nic nie zapisano.\n\n{plan.as_text()}"

    try:
        created = await enricher.execute(plan)
    except EnrichError as exc:
        return f"Nie uzupełniono: {exc}"
    except PartDbError as exc:
        return (
            f"Part-DB odrzucił zapis: {exc}\n\n"
            "Część załączników mogła już powstać — sprawdź przed ponowieniem."
        )

    summary = "\n".join(f"  · {item.describe()}" for item in plan.useful)
    return (
        f"Uzupełnione.\n\n{summary}\n\n"
        f"Utworzone zasoby ({len(created)}): {', '.join(created)}"
    )


@mcp.tool()
async def record_source(
    part: str,
    supplier: str,
    supplier_part_number: str = "",
    product_url: str = "",
    price: float | None = None,
    picture_url: str = "",
    datasheet_url: str = "",
    description: str = "",
    parameters: list[dict] | None = None,
    confirm: bool = False,
) -> str:
    """Zapisz, skąd część pochodzi i po ile: sklep, indeks, link, cena, zdjęcie.

    Dla sklepów, których Part-DB nie zna jako info providerów — **Botland,
    Allegro, AliExpress**. Stamtąd bierze się większość modułów w tym
    warsztacie, a `enrich_part()` ich nie znajdzie, bo szuka wyłącznie
    w katalogach TME i LCSC.

    **Dane zbierasz Ty, nie użytkownik.** Jego rola kończy się na zdjęciu
    wrzuconym do kolejki. Po rozpoznaniu części poszukaj jej w sklepie
    (stronę Botlandu da się odczytać; Allegro odpowiada 403 na automatyczny
    odczyt — wtedy oprzyj się na tym, co widać na zdjęciu, i powiedz
    użytkownikowi, czego zabrakło). Zebrane dane podaj tutaj i pokaż plan.

    **Bez `confirm=true` zwraca sam plan i niczego nie zapisuje.**

    Trzy rzeczy, które to narzędzie robi przy okazji, bo mają znaczenie:

    * **Zdjęcie ze sklepu zjeżdża do Part-DB.** Oferta na Allegro znika po
      miesiącu, a link zostawiłby pustą ramkę.
    * **Indeks sklepu ląduje we właściwym polu.** `KAB-08134` wpisany jako
      numer katalogowy producenta blokuje wyszukiwanie u dystrybutorów —
      gdy pokrywa się z podanym indeksem, przenosimy go do wpisu zakupowego.
    * **Cena zapisuje się przy wpisie zakupowym**, więc przy „kończy się"
      widać, gdzie dokupić i czego się spodziewać.

    Args:
        part: nazwa, numer katalogowy albo identyfikator części.
        supplier: nazwa sklepu (`Botland`, `Allegro`). Utworzy go, jeśli
            jeszcze nie istnieje.
        supplier_part_number: indeks w sklepie (`KAB-08134`).
        product_url: adres karty produktu albo oferty.
        price: cena zapłacona, w złotych.
        picture_url: adres zdjęcia produktu — zostanie pobrane do Part-DB.
        datasheet_url: adres dokumentacji, jeśli sklep ją ma.
        description: krótki opis części, gdy w bazie go brakuje.
        parameters: parametry ze specyfikacji sklepu, każdy jako
            `{"name": "Napięcie zasilania", "value": "5 V"}`.
        confirm: `true` dopiero po akceptacji planu przez użytkownika.
    """
    recorder = SourceRecorder(get_client())
    try:
        plan = await recorder.plan(
            part,
            supplier,
            supplier_part_number=supplier_part_number,
            product_url=product_url,
            price=price,
            picture_url=picture_url,
            datasheet_url=datasheet_url,
            description=description,
            parameters=parameters,
        )
        if not confirm:
            return f"PLAN — nic nie zapisano.\n\n{plan.as_text()}"

        created = await recorder.execute(plan)
        warnings = (
            "\n\nUWAGI:\n" + "\n".join(f"  ! {w}" for w in plan.warnings)
            if plan.warnings
            else ""
        )
        return (
            f"Zapisane źródło: {plan.supplier_name} → {plan.part.get('name')}\n"
            f"Utworzone zasoby ({len(created)}): {', '.join(created)}{warnings}"
        )
    except SourceError as exc:
        return f"Nie zapisano: {exc}"
    except PartDbError as exc:
        return (
            f"Part-DB odrzucił zapis: {exc}\n\n"
            "Część wpisów mogła już powstać — sprawdź przed ponowieniem."
        )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
