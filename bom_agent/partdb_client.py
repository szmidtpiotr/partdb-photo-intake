"""JEDYNY moduł dotykający REST API Part-DB (02-architektura, reguła 4).

Żaden inny moduł `bom_agent` nie importuje `httpx` ani nie składa adresów
API. Wymiana warstwy magazynowej ma ruszać ten jeden plik (ADR-015).

Ustalenia zweryfikowane na żywej instancji v2.15.0 — nie odkrywaj ich od nowa:

* autoryzacja: `Authorization: Bearer tcp_<token>`, token trwały;
* API Platform, JSON-LD: kolekcje mają `hydra:member`, `hydra:totalItems`
  i `hydra:view.hydra:next` — po tym ostatnim iterujemy strony;
* zapis wymaga tokenu poziomu EDIT lub wyższego oraz uprawnienia
  `api.access_api`; token READ_ONLY dostaje **403**, nie 401;
* PATCH w API Platform idzie jako `application/merge-patch+json`;
* `need_pw_change=1` na koncie przekierowuje **wszystkie** żądania, także
  API, na ustawienia użytkownika — objawia się przekierowaniem 302 na HTML;
* `TRUSTED_HOSTS` w Part-DB przepuszcza tylko wpisane tam nazwy (np. domenę
  instancji, adres IP hosta i `localhost`). Odpytanie po nazwie usługi dockerowej
  (`http://partdb`) kończy się `400 Bad Request` — zmierzone.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

JSON_LD = "application/ld+json"
MERGE_PATCH = "application/merge-patch+json"

# API Platform tnie kolekcje po 30 pozycji; 100 to maksimum przyjmowane
# przez Part-DB bez konfiguracji po stronie serwera.
PAGE_SIZE = 100

# Zmierzone na instancji z 330 częściami: `list_parts()` (dedup w
# `intake_commit`/`intake_propose`, ADR-019) kosztuje ~2,3 s — nie po stronie
# sieci, a serializacji hydra każdej części z zagnieżdżonymi `partLots`.
# Rośnie z magazynem, a dedup woła to przy KAŻDYM zatwierdzeniu z telefonu.
# TTL cache jest bezpieczny: dedup działa na nazwie i MPN, a nie na stanie,
# więc kilkanaście sekund nieświeżości nie zmienia wyniku — a każdy zapis
# (część, kategoria, stan) go od razu unieważnia.
LIST_CACHE_TTL = 15.0

JsonDict = dict[str, Any]


class PartDbError(RuntimeError):
    """Błąd komunikacji z Part-DB."""

    def __init__(self, message: str, *, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class PartDbUnavailable(PartDbError):
    """Instancja nie odpowiada: sieć, DNS, timeout, 5xx."""


class PartDbAuthError(PartDbError):
    """401 — brak tokenu albo token nieważny."""


class PartDbForbidden(PartDbError):
    """403 — token ma za niski poziom lub konto nie ma `api.access_api`."""


class PartDbNotFound(PartDbError):
    """404 — zasób nie istnieje."""


class PartDbValidationError(PartDbError):
    """400 / 422 — Part-DB odrzucił treść żądania."""


@dataclass(frozen=True)
class PartDbStatus:
    """Wynik `ping()` — to, co `/health` raportuje o warstwie magazynowej."""

    reachable: bool
    authorized: bool
    latency_ms: float | None = None
    total_parts: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.authorized


def _error_detail(response: httpx.Response) -> str:
    """Wyciągnij `detail` z błędu hydra; przy HTML-u zwróć skrót treści."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200].strip()
    if isinstance(body, dict):
        for key in ("detail", "hydra:description", "description", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text[:200].strip()


class PartDbClient:
    """Asynchroniczny klient REST Part-DB.

    Jedna instancja na proces — trzyma pulę połączeń. Zamykać przez
    `await client.aclose()` albo używać jako async context managera.
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api"
        self._token = token
        headers = {"Accept": JSON_LD, "User-Agent": "bom-agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=timeout,
            # Świadomie NIE podążamy za przekierowaniami: 302 na API to sygnał
            # `need_pw_change`, a nie coś, co warto po cichu obsłużyć.
            follow_redirects=False,
            transport=transport,
        )
        # (znacznik czasu, dane) — patrz `LIST_CACHE_TTL`.
        self._parts_cache: tuple[float, list[JsonDict]] | None = None
        self._categories_cache: tuple[float, list[JsonDict]] | None = None

    @staticmethod
    def iri(resource: str, entity_id: int | str) -> str:
        """IRI zasobu (`/api/parts/7`) — do pól wskazujących w treści zapisu."""
        return f"/api/{resource}/{entity_id}"

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- warstwa transportowa ---------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: JsonDict | None = None,
        content_type: str = JSON_LD,
    ) -> JsonDict | None:
        """Wykonaj żądanie i zamień odpowiedź na dane albo wyjątek.

        `path` przyjmuje zarówno IRI (`/api/parts/1`), jak i ścieżkę
        względem korzenia API (`parts/1`) — obie formy pojawiają się
        naturalnie, bo JSON-LD zwraca `@id` w tej pierwszej.
        """
        url = self._normalize(path)
        headers = {"Content-Type": content_type} if json is not None else None
        try:
            response = await self._http.request(
                method, url, params=params, json=json, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise PartDbUnavailable(
                f"Part-DB nie odpowiedział w czasie: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise PartDbUnavailable(f"Part-DB nieosiągalny: {exc}") from exc
        return self._handle(response)

    def _normalize(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        path = path.lstrip("/")
        if path.startswith("api/"):
            path = path[len("api/") :]
        return f"{self.api_url}/{path}"

    def _handle(self, response: httpx.Response) -> JsonDict | None:
        status = response.status_code
        detail = ""

        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            raise PartDbError(
                "Part-DB przekierowuje żądanie API "
                f"({status} → {location or 'brak Location'}). "
                "Najczęstsza przyczyna: konto tokenu ma need_pw_change=1, "
                "co przekierowuje także API na ustawienia użytkownika.",
                status=status,
            )

        if status >= 400:
            detail = _error_detail(response)
            if status == 401:
                raise PartDbAuthError(
                    f"Part-DB odrzucił token (401): {detail}",
                    status=status,
                    detail=detail,
                )
            if status == 403:
                raise PartDbForbidden(
                    f"Part-DB odmówił dostępu (403): {detail}. "
                    "Sprawdź poziom tokenu (zapis wymaga EDIT lub wyżej) "
                    "oraz uprawnienie api.access_api na koncie.",
                    status=status,
                    detail=detail,
                )
            if status == 404:
                raise PartDbNotFound(
                    f"Part-DB nie zna zasobu {response.url.path} (404)",
                    status=status,
                    detail=detail,
                )
            if status in (400, 409, 422):
                raise PartDbValidationError(
                    f"Part-DB odrzucił treść żądania ({status}): {detail}",
                    status=status,
                    detail=detail,
                )
            if status >= 500:
                raise PartDbUnavailable(
                    f"Part-DB zwrócił błąd serwera ({status}): {detail}",
                    status=status,
                    detail=detail,
                )
            raise PartDbError(
                f"Part-DB zwrócił {status}: {detail}", status=status, detail=detail
            )

        if status == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise PartDbError(
                f"Part-DB zwrócił treść, która nie jest JSON-em: "
                f"{response.text[:200]!r}",
                status=status,
            ) from exc

    # --- odczyt ------------------------------------------------------------

    async def get(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> JsonDict:
        data = await self.request("GET", path, params=params)
        if data is None:
            raise PartDbError(f"Puste ciało odpowiedzi dla GET {path}")
        return data

    async def get_page(
        self,
        resource: str,
        *,
        params: Mapping[str, Any] | None = None,
        page: int = 1,
        per_page: int = PAGE_SIZE,
    ) -> JsonDict:
        """Jedna strona kolekcji, w surowej postaci hydra."""
        query: JsonDict = dict(params or {})
        query.update({"page": page, "itemsPerPage": per_page})
        return await self.get(resource, params=query)

    async def iter_collection(
        self,
        resource: str,
        *,
        params: Mapping[str, Any] | None = None,
        per_page: int = PAGE_SIZE,
    ) -> AsyncIterator[JsonDict]:
        """Przejdź całą kolekcję, idąc za `hydra:view.hydra:next`.

        Ręczne zliczanie stron z `hydra:totalItems` dawałoby ten sam wynik,
        ale link `next` jest tym, co serwer faktycznie deklaruje — i to on
        zniknie, gdy stron zabraknie.
        """
        query: JsonDict = dict(params or {})
        query.setdefault("itemsPerPage", per_page)
        query.setdefault("page", 1)
        next_path: str | None = resource
        next_params: Mapping[str, Any] | None = query

        while next_path is not None:
            page = await self.get(next_path, params=next_params)
            for item in page.get("hydra:member", []):
                yield item
            view = page.get("hydra:view") or {}
            next_path = view.get("hydra:next")
            # `hydra:next` niesie już komplet parametrów w query stringu.
            next_params = None

    async def list_all(
        self, resource: str, *, params: Mapping[str, Any] | None = None
    ) -> list[JsonDict]:
        return [item async for item in self.iter_collection(resource, params=params)]

    async def count(
        self, resource: str, *, params: Mapping[str, Any] | None = None
    ) -> int:
        page = await self.get_page(resource, params=params, per_page=1)
        return int(page.get("hydra:totalItems", 0))

    async def get_part(self, part_id: int | str) -> JsonDict:
        return await self.get(f"parts/{part_id}")

    async def list_parts(
        self, *, params: Mapping[str, Any] | None = None
    ) -> list[JsonDict]:
        """Wszystkie części. `partLots` i `total_instock` są już w wyniku —
        stan i lokalizacja nie wymagają dopytywania po części (N+1).

        Cache'owane na `LIST_CACHE_TTL`, tylko wywołanie bez filtrów —
        to jest ta postać, którą woła dedup (ADR-019) przy każdym
        zatwierdzeniu, a koszt (~2,3 s na 330 częściach, zmierzone) jest
        po stronie serializacji Part-DB, nie sieci, więc nie znika przy
        powtórzeniu. Wywołanie z `params` (filtrowane wyszukiwanie) zawsze
        idzie na żywo — cache miałby tyle wpisów, ile kombinacji filtrów,
        i przestałby być tani.
        """
        if params is not None:
            return await self.list_all("parts", params=params)
        if self._parts_cache is not None:
            stamp, cached = self._parts_cache
            if time.monotonic() - stamp < LIST_CACHE_TTL:
                return cached
        parts = await self.list_all("parts")
        self._parts_cache = (time.monotonic(), parts)
        return parts

    async def list_storage_locations(self) -> list[JsonDict]:
        return await self.list_all("storage_locations")

    async def list_categories(self) -> list[JsonDict]:
        """Cache'owane na `LIST_CACHE_TTL` — patrz `list_parts`."""
        if self._categories_cache is not None:
            stamp, cached = self._categories_cache
            if time.monotonic() - stamp < LIST_CACHE_TTL:
                return cached
        categories = await self.list_all("categories")
        self._categories_cache = (time.monotonic(), categories)
        return categories

    async def list_part_lots(
        self, *, params: Mapping[str, Any] | None = None
    ) -> list[JsonDict]:
        return await self.list_all("part_lots", params=params)

    async def list_attachment_types(self) -> list[JsonDict]:
        return await self.list_all("attachment_types")

    async def list_attachments(self) -> list[JsonDict]:
        return await self.list_all("attachments")

    async def list_suppliers(self) -> list[JsonDict]:
        return await self.list_all("suppliers")

    async def list_orderdetails(self) -> list[JsonDict]:
        """Wpisy zakupowe: u kogo, pod jakim indeksem i po ile."""
        return await self.list_all("orderdetails")

    # --- info providery (TME, LCSC, …) -------------------------------------
    #
    # Odczyt, ale przez REST, a nie przez wbudowany MCP: te dane są wsadem do
    # NASZEGO zapisu (zdjęcie i datasheet przy części), więc muszą być
    # dostępne w kodzie, a nie tylko w kontekście sesji. ADR-016 zabrania
    # dublowania odczytu narzędziami MCP — a nie sięgania po dane w kodzie.

    async def list_info_providers(self) -> list[JsonDict]:
        page = await self.get("info_providers")
        return list(page.get("hydra:member", []))

    async def search_info_providers(
        self, keyword: str, providers: list[str] | None = None
    ) -> list[JsonDict]:
        """Szukaj części u dostawców. Puste `providers` = domyślne z ustawień.

        Uwaga na instancji warsztatowej: domyślne providery **nie są
        ustawione**, więc wywołanie bez listy kończy się błędem. Kto woła,
        podaje klucze wprost (`["lcsc", "tme"]`).
        """
        payload: JsonDict = {"keyword": keyword}
        if providers:
            payload["providers"] = list(providers)
        result = await self.create("info_providers/search", payload)
        return list(result.get("hydra:member", []))

    async def info_provider_details(
        self, provider_key: str, provider_id: str
    ) -> JsonDict:
        """Pełne dane części u dostawcy: zdjęcia, datasheety, parametry.

        Wyszukiwanie zwraca tylko miniaturę (`preview_image_url`); pełne
        zdjęcia są dopiero tutaj — i zależą od dostawcy. Zmierzone na
        instancji: LCSC oddaje 900×900, TME nie oddaje żadnego.
        """
        return await self.create(
            "info_providers/details",
            {"provider_key": provider_key, "provider_id": provider_id},
        )

    async def create_parameter(self, payload: JsonDict) -> JsonDict:
        return await self.create("parameters", payload)

    # --- zapis -------------------------------------------------------------
    #
    # Wbudowany MCP Part-DB jest tylko do odczytu (ADR-016), więc każdy zapis
    # przechodzi tędy. Do działania potrzeba tokenu poziomu EDIT lub wyżej.

    async def create(self, resource: str, payload: JsonDict) -> JsonDict:
        data = await self.request("POST", resource, json=payload)
        if data is None:
            raise PartDbError(f"POST {resource} nie zwrócił utworzonego zasobu")
        self._invalidate_list_cache(resource)
        return data

    async def patch(self, iri: str, payload: JsonDict) -> JsonDict:
        data = await self.request("PATCH", iri, json=payload, content_type=MERGE_PATCH)
        if data is None:
            raise PartDbError(f"PATCH {iri} nie zwrócił zasobu po zmianie")
        self._invalidate_list_cache(iri)
        return data

    async def delete(self, iri: str) -> None:
        await self.request("DELETE", iri)
        self._invalidate_list_cache(iri)

    def _invalidate_list_cache(self, resource_or_iri: str) -> None:
        """Zapis unieważnia cache — patrz `LIST_CACHE_TTL`.

        `part_lots` unieważnia też części: `list_parts()` niesie zagnieżdżone
        `partLots` (żeby dedup i digest nie robiły N+1), więc nowy stan
        zmienia treść, którą cache parts trzyma.
        """
        if "categories" in resource_or_iri:
            self._categories_cache = None
        if "parts" in resource_or_iri or "part_lots" in resource_or_iri:
            self._parts_cache = None

    async def create_part(self, payload: JsonDict) -> JsonDict:
        return await self.create("parts", payload)

    async def create_part_lot(self, payload: JsonDict) -> JsonDict:
        return await self.create("part_lots", payload)

    async def update_part_lot(self, lot_id: int | str, payload: JsonDict) -> JsonDict:
        return await self.patch(f"part_lots/{lot_id}", payload)

    async def create_attachment(self, payload: JsonDict) -> JsonDict:
        """Załącznik przy części. `url` zostaje odnośnikiem — Part-DB nie
        pobiera pliku do siebie, więc zdjęcie intake'u nie leży w dwóch
        miejscach naraz. Wyświetlanie działa, bo CSP Part-DB ma `img-src *`
        (sprawdzone na instancji v2.15) — obrazek z innego hosta się pokaże."""
        return await self.create("attachments", payload)

    async def fetch_media(self, path: str) -> tuple[bytes, str]:
        """Plik załącznika trzymany w Part-DB (`/media/…`): treść i typ.

        Front Warsztatu (ADR-022) pokazuje zdjęcia i datasheety bez tokenu
        Part-DB, więc idą przez nas. Przejście jest wąskie celowo: tylko
        ścieżki `/media/`, bez `..`, inaczej stałoby się ogólnym proxy do
        całej instancji.
        """
        if not path.startswith("/media/") or ".." in path:
            raise PartDbError(f"Nie przepuszczam ścieżki {path!r} — tylko /media/")
        try:
            response = await self._http.get(f"{self.base_url}{path}")
        except httpx.HTTPError as exc:
            raise PartDbUnavailable(f"Part-DB nieosiągalny: {exc}") from exc
        if response.status_code != 200:
            raise PartDbError(
                f"Part-DB zwrócił {response.status_code} dla {path}",
                status=response.status_code,
            )
        return response.content, response.headers.get(
            "content-type", "application/octet-stream"
        )

    # --- diagnostyka -------------------------------------------------------

    async def ping(self) -> PartDbStatus:
        """Sprawdź dostępność i ważność tokenu jednym tanim żądaniem.

        `/api/parts?itemsPerPage=1` jest lepszym probem niż korzeń API:
        korzeń bywa dostępny bez autoryzacji, więc nie odróżniłby
        „instancja żyje" od „token działa".
        """
        if not self.has_token:
            return PartDbStatus(
                reachable=False,
                authorized=False,
                error="Brak PARTDB_API_TOKEN — nie ma czym się uwierzytelnić",
            )
        started = time.perf_counter()
        try:
            page = await self.get_page("parts", per_page=1)
        except (PartDbAuthError, PartDbForbidden) as exc:
            return PartDbStatus(
                reachable=True,
                authorized=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                error=str(exc),
            )
        except PartDbError as exc:
            return PartDbStatus(reachable=False, authorized=False, error=str(exc))
        return PartDbStatus(
            reachable=True,
            authorized=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            total_parts=int(page.get("hydra:totalItems", 0)),
        )
