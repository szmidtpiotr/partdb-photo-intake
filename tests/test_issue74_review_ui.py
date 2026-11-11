"""Issue #74 — zakładka „Do zatwierdzenia" na telefonie.

Dwa poziomy, bo dwie różne awarie:

* **statyczny** — czy przycisk ma obsługę, a zakładka odznakę. Klasyczny
  błąd tej aplikacji to element w HTML bez ani jednej linijki kodu za nim;
  test bez przeglądarki łapie to za darmo.
* **przeglądarkowy** — czy karta naprawdę się rysuje, da się poprawić
  ilość i odrzucić propozycję. Uruchamiany z adresem instancji, jak
  `test_pwa_offline_e2e`:

      BOM_E2E_URL=https://intake.example.com \\
          .venv/bin/python -m pytest tests/test_issue74_review_ui.py

Test przeglądarkowy **nie zatwierdza** propozycji — zatwierdzenie pisze do
magazynu, a zaśmiecanie realnego Part-DB próbnymi częściami byłoby gorsze
niż brak testu. Zapis pokrywają testy `test_issue73_proposals`.
"""

import os
import re
from pathlib import Path

import pytest

PWA = Path(__file__).resolve().parent.parent / "pwa"
INDEX = (PWA / "index.html").read_text(encoding="utf-8")
APP = (PWA / "app.js").read_text(encoding="utf-8")
CSS = (PWA / "styles.css").read_text(encoding="utf-8")
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

BASE_URL = os.environ.get("BOM_E2E_URL", "").rstrip("/")


# ─── Wiring: element bez obsługi to element, który nie działa ────────────────


def test_review_tab_exists_with_its_badge():
    assert 'id="tab-review"' in INDEX
    assert 'id="badge-review"' in INDEX


def test_review_tab_is_wired_to_a_handler():
    assert "$('tab-review').addEventListener" in APP
    assert "setTab('review')" in APP


def test_cards_come_from_the_proposals_endpoint():
    assert "/api/proposals?status=pending" in APP


def test_card_edits_go_through_patch():
    """Poprawka ilości musi trafić na serwer, a nie zostać w polu."""
    assert "method: 'PATCH'" in APP
    assert "/api/proposals/${proposal.id}`" in APP


def test_approve_and_reject_hit_their_endpoints():
    assert "/api/proposals/${proposal.id}/${action}" in APP
    assert "'approve'" in APP and "'reject'" in APP


def test_approve_all_exists_for_group_photos():
    """Jedno zdjęcie daje kilka propozycji (#72) — klikanie ✓ pięć razy
    jest pracą bez treści."""
    assert "/api/intake/${itemId}/approve" in APP


def test_recognize_button_is_a_toggle():
    """Zlecenie musi dać się zdjąć: pomyłka w kliknięciu nie może kosztować
    tokenów na rozpoznanie niekompletnego zdjęcia."""
    assert "recognize?wanted=" in APP
    assert "'Zlecenie zdjęte'" in APP


def test_thumbnail_uses_the_url_stored_with_the_proposal():
    """Miniatura to wycinek policzony przy rozpoznaniu (#72), nie cały kadr."""
    assert "proposal.photo_url" in APP


def test_verdict_is_shown_on_the_card():
    assert "dołoży stan do" in APP
    assert "utworzy nową część" in APP


def test_card_styles_exist():
    """Karta bez stylu na telefonie to nieczytelna kolumna tekstu."""
    for selector in (".card {", ".card-field input {", ".verdict {", ".recognize {"):
        assert selector in RULES, f"brak reguły {selector}"


def test_inputs_are_at_least_16px_so_ios_does_not_zoom():
    block = RULES.split(".card-field input {")[1].split("}")[0]
    size = re.search(r"font-size:\s*(\d+)px", block)
    assert size and int(size.group(1)) >= 16


def test_items_awaiting_review_leave_the_queue_tab():
    """Pozycja z propozycjami nie jest „do zrobienia" — zostawienie jej
    w „Czeka" prosiłoby o rozpoznanie rzeczy właśnie rozpoznanej."""
    assert "awaiting.has(i.id)" in APP


# ─── Przeglądarka ───────────────────────────────────────────────────────────

e2e = pytest.mark.skipif(
    not BASE_URL, reason="ustaw BOM_E2E_URL, żeby uruchomić test przeglądarkowy"
)

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
        context = browser.new_context(viewport={"width": 412, "height": 915})
        page = context.new_page()
        yield page
        browser.close()


def api(page, path, options="{}"):
    return page.evaluate(
        f"async (base) => {{"
        f"  const r = await fetch(base + '{path}', {options});"
        f"  return {{ ok: r.ok, status: r.status, body: await r.json() }};"
        f"}}",
        BASE_URL,
    )


@e2e
def test_review_tab_shows_editable_cards_for_pending_proposals(page):
    """Karta ma się narysować i przyjąć poprawkę ilości.

    Propozycji nie zakładamy z przeglądarki — pisze je agent narzędziem
    `intake_propose`. Test pracuje na tym, co w instancji czeka; brak
    propozycji znaczy „nie ma czego klikać", a nie porażkę.
    """
    page.goto(BASE_URL, wait_until="networkidle")
    page.click("#tab-review")
    page.wait_for_timeout(500)

    cards = page.query_selector_all(".card")
    if not cards:
        assert "Nic nie czeka na zatwierdzenie" in page.inner_text("#empty")
        pytest.skip("brak propozycji w instancji — nie ma czego klikać")

    before = api(page, "/api/proposals?status=pending")["body"]["proposals"][0]
    amount_field = cards[0].query_selector_all(".card-field input")[1]
    original = amount_field.input_value()

    try:
        amount_field.fill("3")
        amount_field.dispatch_event("change")
        page.wait_for_timeout(800)

        after = api(page, f"/api/proposals?item_id={before['item_id']}")
        patched = next(p for p in after["body"]["proposals"] if p["id"] == before["id"])
        assert str(patched["payload"]["amount"]) == "3", (
            "poprawka ilości została w polu, zamiast trafić na serwer"
        )
        # Werdykt deduplikacji musi być widoczny obok pól — bez niego nie
        # wiadomo, czy ✓ tworzy część, czy dokłada stan do istniejącej.
        assert cards[0].query_selector(".verdict") is not None
    finally:
        page.evaluate(
            """async ([base, id, value]) => {
                await fetch(base + '/api/proposals/' + id, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ amount: Number(value) }),
                });
            }""",
            [BASE_URL, before["id"], original or "1"],
        )


@e2e
def test_recognize_button_marks_the_item_on_the_server(page):
    page.goto(BASE_URL, wait_until="networkidle")

    item_id = page.evaluate(
        """async ([base, bytes]) => {
            const body = new FormData();
            const blob = new Blob([new Uint8Array(bytes)], { type: 'image/jpeg' });
            body.append('photo', blob, 'e2e.jpg');
            const r = await fetch(base + '/api/intake/photo', { method: 'POST', body });
            return (await r.json()).id;
        }""",
        [BASE_URL, list(JPEG)],
    )

    try:
        page.reload(wait_until="networkidle")
        page.click(f'.recognize[aria-label*="pozycja {item_id}"]')
        page.wait_for_timeout(800)

        state = api(page, "/api/intake?limit=50")["body"]["items"]
        row = next(item for item in state if item["id"] == item_id)
        assert row["recognize_requested_at"], "zlecenie nie doszło na serwer"
    finally:
        api(page, f"/api/intake/{item_id}", "{method: 'DELETE'}")


# ─── Spike #76: aparat w aplikacji z zapamiętanym zoomem ─────────────────────


def test_precise_mode_is_a_toggle_not_a_replacement():
    """Natywny aparat daje pełne przetwarzanie telefonu i zostaje domyślny,
    dopóki porównanie na sitodruku nie powie inaczej."""
    assert 'id="precise"' in INDEX
    assert 'capture="environment"' in INDEX, "natywna droga musi zostać"
    assert "preciseMode()" in APP


def test_zoom_is_remembered_between_shots():
    """Cały sens spike'u: nie ustawiać przybliżenia przy każdym zdjęciu."""
    assert "ZOOM_KEY" in APP
    assert "localStorage.setItem(ZOOM_KEY" in APP
    assert "localStorage.getItem(ZOOM_KEY" in APP


def test_zoom_goes_through_track_constraints():
    assert "applyConstraints({ advanced: [{ zoom }] })" in APP


def test_capture_prefers_full_resolution_over_a_preview_frame():
    """Klatka z <video> ma tylko tyle pikseli, ile podgląd — a to jest ta
    różnica, przez którą drobny nadruk staje się nieczytelny."""
    assert "ImageCapture" in APP
    assert "takePhoto" in APP
    assert "canvas.toBlob" in APP, "musi być droga awaryjna"


def test_camera_without_zoom_support_says_so():
    """Suwak, który nic nie robi, jest gorszy niż jego brak."""
    assert "nie pozwala go ustawić" in APP


def test_missing_camera_permission_falls_back_to_the_native_button():
    assert "Aparat w aplikacji niedostępny" in APP


def test_stream_is_stopped_when_the_viewfinder_closes():
    """Niezwolniony strumień zostawia zapaloną diodę aparatu i zjada baterię."""
    assert "track.stop()" in APP


@e2e
def test_batch_bar_is_not_visible_on_the_review_tab(page):
    """Zgłoszone dwa razy: pasek „Zleć rozpoznanie" wisiał na wierzchu karty
    propozycji i zasłaniał „✓ Zatwierdź". Pierwsza poprawka ustawiała
    `hidden` poprawnie, ale `.batch-bar { display: flex }` bije `[hidden]`
    z arkusza przeglądarki — więc przełącznik był martwy. Test sprawdza
    WIDOCZNOŚĆ w przeglądarce, nie obecność atrybutu w kodzie."""
    page.goto(BASE_URL, wait_until="networkidle")

    # Pasek nie ma prawa wisieć nawet zaraz po wejściu, bez zaznaczeń.
    assert page.is_hidden("#batch-bar"), "pasek widoczny bez żadnego zaznaczenia"

    boxes = page.query_selector_all(".select-cell input")
    if boxes:
        boxes[0].check()
        page.wait_for_timeout(200)
        assert page.is_visible("#batch-bar"), (
            "po zaznaczeniu pozycji w zakladce Czeka pasek ma sie pokazac"
        )

    page.click("#tab-review")
    page.wait_for_timeout(400)
    assert page.is_hidden("#batch-bar"), (
        "pasek zostal widoczny na zakladce Do zatwierdzenia"
    )

    # I nie może przechwytywać dotknięć przycisku na karcie.
    approve = page.query_selector('.card-actions button[data-action="approve"]')
    if approve:
        box = approve.bounding_box()
        top = page.evaluate(
            "([x, y]) => { const el = document.elementFromPoint(x, y);"
            " const hit = el && el.closest('#batch-bar');"
            " return hit ? 'batch-bar' : (el ? el.tagName : 'brak'); }",
            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
        )
        assert top != "batch-bar", "pasek nadal przechwytuje kliknięcie ✓"
