"""Poczekalnia w prawdziwej przeglądarce (#60).

Warunek z issue brzmiał dosłownie: *tryb samolotowy → zrobienie zdjęcia →
zamknięcie aplikacji → włączenie sieci → zdjęcie samo trafia do kolejki*.
IndexedDB, service worker i zdarzenie `online` istnieją tylko w
przeglądarce, więc jedyny uczciwy sposób sprawdzenia tego to uruchomienie
aplikacji w przeglądarce.

Test **nie wchodzi do zwykłego przebiegu** — potrzebuje działającej
usługi. Uruchamiany jest z adresem instancji:

    BOM_E2E_URL=https://intake.example.com \
        .venv/bin/python -m pytest tests/test_pwa_offline_e2e.py

Sprząta po sobie: pozycje utworzone w trakcie są kasowane przez API.
"""

import os

import pytest

BASE_URL = os.environ.get("BOM_E2E_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="ustaw BOM_E2E_URL, żeby uruchomić test przeglądarkowy"
)

# Najmniejszy poprawny JPEG — Part-DB go nie ogląda, a kolejka sprawdza
# tylko typ i rozmiar. Duży plik spowalniałby test bez żadnego zysku.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
    "c00011080001000103012200021101031101ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b5100002010303020403050504"
    "040000017d01020300041105122131410613516107227114328191a1082342b1c115"
    "52d1f02433627282090a161718191a25262728292a3435363738393a434445464748"
    "494a535455565758595a636465666768696a737475767778797a83848586878889"
    "8a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7"
    "c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda"
    "0008010100003f00bf8a28a2803fffd9"
)


@pytest.fixture
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        yield page
        browser.close()


def expected_version() -> int:
    import re
    from pathlib import Path

    app = (Path(__file__).resolve().parent.parent / "pwa" / "app.js").read_text(
        encoding="utf-8"
    )
    return int(re.search(r"const APP_VERSION = (\d+);", app).group(1))


def queue_ids(page) -> set[int]:
    items = page.evaluate(
        "async (base) => (await (await fetch(base + '/api/intake?limit=50')).json())"
        ".items.map((i) => i.id)",
        BASE_URL,
    )
    return set(items)


def test_photo_taken_offline_survives_a_restart_and_sends_itself(page):
    page.goto(BASE_URL, wait_until="networkidle")
    # Instancja musi mieć wgrany ten kod, a nie poprzedni — inaczej test
    # sprawdzałby cudzą wersję i mówił o niej „działa".
    assert f"v{expected_version()}" in page.locator("#status").inner_text()
    before = queue_ids(page)

    # 1. Tryb samolotowy.
    page.context.set_offline(True)

    # 2. Zdjęcie. `set_input_files` odpowiada wyborowi w wybieraku telefonu.
    page.set_input_files(
        "#camera",
        {"name": "test-offline.jpg", "mimeType": "image/jpeg", "buffer": JPEG},
    )
    page.wait_for_selector(".item .tag[data-status='failed'], .item .retry")
    assert "nie wysłano" in page.locator("#counts").inner_text()

    # 3. Zamknięcie aplikacji: nowa karta to nowa pamięć JavaScriptu,
    #    a właśnie ona gubiła zdjęcia przed tą zmianą.
    page.close()
    page = page.context.new_page()
    page.context.set_offline(True)
    page.goto(BASE_URL)
    page.wait_for_selector(".item .retry")

    # 4. Sieć wraca — i nikt niczego nie dotyka.
    page.context.set_offline(False)
    page.evaluate("() => window.dispatchEvent(new Event('online'))")

    page.wait_for_function(
        "() => document.querySelectorAll('.item .retry').length === 0", timeout=15000
    )
    after = queue_ids(page)
    new_items = after - before
    assert len(new_items) == 1, f"oczekiwano jednej nowej pozycji, jest {new_items}"

    # Sprzątanie: to była proteza, nie część warsztatu.
    for item_id in new_items:
        page.evaluate(
            "async ([base, id]) => fetch(`${base}/api/intake/${id}`, "
            "{ method: 'DELETE' })",
            [BASE_URL, item_id],
        )


def test_photo_rejected_by_the_server_stays_visible_with_the_reason(page):
    """Odrzucenie to nie utrata. Plik z niedozwolonym typem ma zostać
    w poczekalni z powodem, ale nie ma być ponawiany w nieskończoność."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.set_input_files(
        "#camera",
        {"name": "notatka.txt", "mimeType": "text/plain", "buffer": b"to nie zdjecie"},
    )

    page.wait_for_selector(".item .retry")
    note = page.locator(".item .sub").first.inner_text()
    assert "odrzucone" in note.lower() or "nie jest obsługiwany" in note

    # Wpis znika dopiero na życzenie użytkownika.
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".item .delete").first.click()
    page.wait_for_function(
        "() => document.querySelectorAll('.item .retry').length === 0"
    )


def test_shared_photo_lands_in_the_queue(page):
    """#65: „Udostępnij → Warsztat" z Lensa albo ze Zdjęć. Android wysyła
    POST na `/share`, który przechwytuje service worker — żądanie nigdy nie
    wychodzi z telefonu, więc udostępnianie działa też bez zasięgu."""
    page.goto(BASE_URL, wait_until="networkidle")
    before = queue_ids(page)

    # Bez aktywnego service workera nie ma czego testować: to on jest
    # jedynym odbiorcą udostępnienia.
    page.wait_for_function("() => navigator.serviceWorker.controller !== null")

    landed = page.evaluate(
        """async (jpeg) => {
            const form = new FormData();
            const blob = new Blob([new Uint8Array(jpeg)], { type: 'image/jpeg' });
            form.append('photos', blob, 'z-lensa.jpg');
            const response = await fetch('/share', { method: 'POST', body: form });
            return response.url;
        }""",
        list(JPEG),
    )
    assert "udostepniono=1" in landed

    # Zdjęcie czeka w poczekalni, a nie zniknęło po drodze.
    page.goto(landed)
    page.wait_for_function(
        "() => document.querySelectorAll('.item').length > 0", timeout=15000
    )

    # Aplikacja wysyła je sama przy otwarciu.
    page.wait_for_function(
        "() => document.querySelectorAll('.item .retry').length === 0", timeout=15000
    )
    new_items = queue_ids(page) - before
    assert len(new_items) == 1, f"oczekiwano jednej nowej pozycji, jest {new_items}"

    for item_id in new_items:
        page.evaluate(
            "async ([base, id]) => fetch(`${base}/api/intake/${id}`, "
            "{ method: 'DELETE' })",
            [BASE_URL, item_id],
        )


def test_shared_text_without_a_photo_prefills_the_dictation_field(page):
    """Ze sklepu przychodzi czasem sam adres. Nie zgadujemy, co to za część —
    tekst ląduje w polu opisu i to użytkownik decyduje, co z nim zrobić."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_function("() => navigator.serviceWorker.controller !== null")

    landed = page.evaluate(
        """async () => {
            const form = new FormData();
            form.append('title', 'Zasilacz AC-DC 5V');
            form.append('url', 'https://sklep.example/modul');
            const response = await fetch('/share', { method: 'POST', body: form });
            return response.url;
        }"""
    )
    assert "tekst=" in landed

    page.goto(landed)
    page.wait_for_function("() => document.getElementById('text').value.length > 0")
    text = page.locator("#text").input_value()
    assert "Zasilacz AC-DC 5V" in text
    assert "sklep.example" in text
    # Nic nie zostało wysłane bez decyzji użytkownika.
    assert page.locator(".item .retry").count() == 0
