"""Testy klienta na `httpx.MockTransport` — bez sieci i bez żywej instancji.

Kształty odpowiedzi przepisane z realnej instancji Part-DB v2.15.0
(`curl` na żywej instancji), nie wymyślone.
"""

import httpx
import pytest

from bom_agent.partdb_client import (
    JSON_LD,
    MERGE_PATCH,
    PartDbAuthError,
    PartDbClient,
    PartDbError,
    PartDbForbidden,
    PartDbNotFound,
    PartDbUnavailable,
    PartDbValidationError,
)

BASE = "http://partdb.test"


def hydra_error(status: int, detail: str) -> dict:
    return {
        "@context": "/api/contexts/Error",
        "@type": "hydra:Error",
        "detail": detail,
        "status": status,
        "hydra:description": detail,
    }


def client(handler, token: str = "tcp_test") -> PartDbClient:
    return PartDbClient(BASE, token, transport=httpx.MockTransport(handler))


async def test_get_part_sends_bearer_and_json_ld():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, json={"@id": "/api/parts/1", "name": "ESP32"})

    async with client(handler) as c:
        part = await c.get_part(1)

    assert part["name"] == "ESP32"
    assert seen["url"] == f"{BASE}/api/parts/1"
    assert seen["auth"] == "Bearer tcp_test"
    assert seen["accept"] == JSON_LD


@pytest.mark.parametrize(
    "path", ["/api/parts/1", "api/parts/1", "parts/1", f"{BASE}/api/parts/1"]
)
async def test_iri_and_relative_paths_resolve_identically(path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"@id": "/api/parts/1"})

    async with client(handler) as c:
        await c.get(path)

    assert seen == [f"{BASE}/api/parts/1"]


async def test_iter_collection_follows_hydra_next():
    pages = {
        1: {
            "hydra:totalItems": 3,
            "hydra:member": [{"id": 1}, {"id": 2}],
            "hydra:view": {"hydra:next": "/api/parts?itemsPerPage=2&page=2"},
        },
        2: {
            "hydra:totalItems": 3,
            "hydra:member": [{"id": 3}],
            "hydra:view": {"hydra:first": "/api/parts?itemsPerPage=2&page=1"},
        },
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        requested.append(page)
        return httpx.Response(200, json=pages[page])

    async with client(handler) as c:
        parts = await c.list_parts()

    assert [p["id"] for p in parts] == [1, 2, 3]
    assert requested == [1, 2]


async def test_count_reads_total_items():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["itemsPerPage"] == "1"
        return httpx.Response(200, json={"hydra:totalItems": 25, "hydra:member": []})

    async with client(handler) as c:
        assert await c.count("parts") == 25


async def test_post_uses_json_ld_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(201, json={"@id": "/api/part_lots/9"})

    async with client(handler) as c:
        created = await c.create_part_lot({"amount": 5})

    assert created["@id"] == "/api/part_lots/9"
    assert seen["method"] == "POST"
    assert seen["content_type"] == JSON_LD


async def test_patch_uses_merge_patch_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"@id": "/api/part_lots/1", "amount": 4})

    async with client(handler) as c:
        await c.update_part_lot(1, {"amount": 4})

    assert seen["method"] == "PATCH"
    assert seen["content_type"] == MERGE_PATCH


async def test_delete_accepts_empty_204():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    async with client(handler) as c:
        assert await c.delete("/api/part_lots/1") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, PartDbAuthError),
        (403, PartDbForbidden),
        (404, PartDbNotFound),
        (400, PartDbValidationError),
        (422, PartDbValidationError),
        (500, PartDbUnavailable),
        (418, PartDbError),
    ],
)
async def test_status_codes_map_to_exceptions(status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=hydra_error(status, "Access Denied."))

    async with client(handler) as c:
        with pytest.raises(expected):
            await c.get("parts/1")


async def test_403_message_points_at_token_level():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=hydra_error(403, "Access Denied."))

    async with client(handler) as c:
        with pytest.raises(PartDbForbidden, match="EDIT"):
            await c.create_part({"name": "x"})


async def test_redirect_is_reported_as_need_pw_change_trap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/user/settings"})

    async with client(handler) as c:
        with pytest.raises(PartDbError, match="need_pw_change"):
            await c.get("parts")


async def test_html_body_is_not_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Untrusted Host</html>")

    async with client(handler) as c:
        with pytest.raises(PartDbError, match="nie jest JSON"):
            await c.get("parts")


async def test_network_failure_becomes_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with client(handler) as c:
        with pytest.raises(PartDbUnavailable):
            await c.get("parts")


async def test_timeout_becomes_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    async with client(handler) as c:
        with pytest.raises(PartDbUnavailable):
            await c.get("parts")


async def test_ping_ok_reports_part_count():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hydra:totalItems": 25, "hydra:member": []})

    async with client(handler) as c:
        status = await c.ping()

    assert status.ok is True
    assert status.total_parts == 25
    assert status.latency_ms is not None


async def test_ping_without_token_does_not_call_network():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("bez tokenu nie ma po co pytać")

    async with client(handler, token="") as c:
        status = await c.ping()

    assert status.reachable is False
    assert status.authorized is False
    assert "PARTDB_API_TOKEN" in status.error


async def test_ping_with_read_only_token_is_reachable_but_unauthorized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=hydra_error(403, "Access Denied."))

    async with client(handler) as c:
        status = await c.ping()

    assert status.reachable is True
    assert status.authorized is False
    assert status.ok is False


async def test_ping_when_instance_is_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with client(handler) as c:
        status = await c.ping()

    assert status.reachable is False
    assert "nieosiągalny" in status.error


# ─── Cache list_parts/list_categories (ADR-019 dedup, zmierzone ~2,3 s) ─────
#
# Na instancji z 330 częściami `list_parts()` bez cache kosztuje ~2,3 s —
# nie po stronie sieci, a serializacji hydra po stronie Part-DB. Dedup woła
# to przy KAŻDYM zatwierdzeniu propozycji z telefonu (#73), więc bez cache
# każde ✓ na karcie płaci ten koszt od nowa, choćby magazyn się nie zmienił.


async def test_list_parts_is_served_from_cache_on_repeated_calls():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = {"hydra:totalItems": 1, "hydra:member": [{"id": 1}]}
        return httpx.Response(200, json=body)

    async with client(handler) as c:
        first = await c.list_parts()
        second = await c.list_parts()

    assert first == second
    assert len(calls) == 1, "drugie wywołanie miało trafić w cache, nie w sieć"


async def test_list_categories_is_served_from_cache_on_repeated_calls():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = {"hydra:totalItems": 1, "hydra:member": [{"id": 1}]}
        return httpx.Response(200, json=body)

    async with client(handler) as c:
        await c.list_categories()
        await c.list_categories()

    assert len(calls) == 1


async def test_filtered_list_parts_bypasses_the_cache():
    """Cache na wywołanie bez filtrów miałby tyle wpisów, ile kombinacji
    parametrów wyszukiwania, i przestałby być tani — filtrowane zapytania
    idą zawsze na żywo."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("name"))
        return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

    async with client(handler) as c:
        await c.list_parts(params={"name": "HC-SR04"})
        await c.list_parts(params={"name": "HC-SR04"})

    assert calls == ["HC-SR04", "HC-SR04"], "wywołanie z params nie ma trafiać w cache"


async def test_creating_a_part_invalidates_the_cache():
    """Bez tego druga część utworzona chwilę po pierwszej nie zobaczy jej
    w dedupie i #70 (fałszywe scalenie różnych modułów) wróciłoby po cichu."""
    listings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"@id": "/api/parts/9", "name": "Nowa"})
        listings.append(1)
        return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

    async with client(handler) as c:
        await c.list_parts()
        await c.create_part({"name": "Nowa", "category": "/api/categories/1"})
        await c.list_parts()

    assert len(listings) == 2, "po zapisie druga lista miała pójść na żywo"


async def test_creating_a_part_lot_invalidates_the_parts_cache():
    """`list_parts()` niesie zagnieżdżone `partLots` — nowy stan zmienia
    treść, którą cache trzyma, nawet gdy sama część się nie zmieniła."""
    listings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"@id": "/api/part_lots/5"})
        listings.append(1)
        return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

    async with client(handler) as c:
        await c.list_parts()
        await c.create_part_lot({"part": "/api/parts/1", "amount": 3})
        await c.list_parts()

    assert len(listings) == 2


async def test_patching_a_part_invalidates_the_cache():
    listings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(200, json={"@id": "/api/parts/1"})
        listings.append(1)
        return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

    async with client(handler) as c:
        await c.list_parts()
        await c.patch("/api/parts/1", {"needs_review": True})
        await c.list_parts()

    assert len(listings) == 2


async def test_creating_a_category_invalidates_only_the_categories_cache():
    part_listings = []
    category_listings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"@id": "/api/categories/9"})
        if "categories" in str(request.url):
            category_listings.append(1)
        else:
            part_listings.append(1)
        return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

    async with client(handler) as c:
        await c.list_parts()
        await c.list_categories()
        await c.create("categories", {"name": "Nowa kategoria"})
        await c.list_parts()
        await c.list_categories()

    assert len(part_listings) == 1, "kategorie nie mają unieważniać cache części"
    assert len(category_listings) == 2


async def test_cache_expires_after_its_ttl():
    import bom_agent.partdb_client as module

    calls = []
    fake_now = [1000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

    async with client(handler) as c:
        original_monotonic = module.time.monotonic
        module.time.monotonic = lambda: fake_now[0]
        try:
            await c.list_parts()
            fake_now[0] += module.LIST_CACHE_TTL + 1
            await c.list_parts()
        finally:
            module.time.monotonic = original_monotonic

    assert len(calls) == 2, "po wygaśnięciu TTL kolejne wywołanie miało pójść na żywo"
