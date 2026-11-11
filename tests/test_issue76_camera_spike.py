"""Spike #76 — aparat w aplikacji z zapamiętanym zoomem.

Natywna aplikacja aparatu (`capture="environment"`, decyzja z 06-intake)
startuje z zoomem 1× i nie ma API, żeby jej podać stan początkowy. Przy
drobnym sitodruku znaczy to ustawianie przybliżenia przy każdym zdjęciu od
nowa.

Własny podgląd pozwala zoom zapamiętać, ale płaci surową klatką z sensora
zamiast pełnego przetwarzania telefonu — a czytelność nadruku jest jedyną
rzeczą, po której rozpoznaje się moduł (ADR-005). Dlatego to **przełącznik,
nie zamiana**, i dlatego testy pilnują głównie tego, żeby droga natywna
została nietknięta.

Czego te testy NIE sprawdzają: jakości zdjęcia. Tego nie da się sprawdzić
w Chromium bez kamery — rozstrzyga jedno porównanie na realnej płytce,
opisane w #76.
"""

import re
from pathlib import Path

PWA = Path(__file__).resolve().parent.parent / "pwa"
INDEX = (PWA / "index.html").read_text(encoding="utf-8")
APP = (PWA / "app.js").read_text(encoding="utf-8")
CSS = (PWA / "styles.css").read_text(encoding="utf-8")
# Same reguły, bez komentarzy: komentarze cytują selektory (np. przy opisie
# błędu z `[hidden]`), a `CSS.split(".viewfinder {")` trafiałby wtedy w cytat
# zamiast w regułę.
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def test_native_camera_stays_the_default_path():
    """Spike nie może odebrać niczego, co dziś działa."""
    assert 'capture="environment"' in INDEX
    assert 'id="camera"' in INDEX
    assert "localStorage.getItem(PRECISE_KEY) === '1'" in APP, (
        "tryb precyzyjny musi być domyślnie wyłączony"
    )


def test_toggle_is_persisted_between_visits():
    assert 'id="precise"' in INDEX
    assert "localStorage.setItem(PRECISE_KEY" in APP


def test_zoom_survives_closing_the_app():
    """Cały sens spike'u: nie ustawiać przybliżenia przy każdym zdjęciu."""
    assert "localStorage.setItem(ZOOM_KEY" in APP
    assert "localStorage.getItem(ZOOM_KEY" in APP


def test_zoom_is_applied_through_track_constraints():
    assert "applyConstraints({ advanced: [{ zoom }] })" in APP


def test_saved_zoom_is_clamped_to_what_the_camera_supports():
    """Zapamiętane 5× na aparacie, który daje 3×, skończyłoby się odrzuconym
    `applyConstraints` i podglądem bez przybliżenia."""
    assert "Math.min(Math.max(saved, caps.zoom.min), caps.zoom.max)" in APP


def test_capture_prefers_a_real_photo_over_a_preview_frame():
    """Klatka z <video> ma tylko tyle pikseli, ile podgląd — a to jest ta
    różnica, przez którą drobny nadruk staje się nieczytelny."""
    assert "ImageCapture" in APP
    assert "takePhoto" in APP
    assert "canvas.toBlob" in APP, "musi zostać droga awaryjna"


def test_requested_resolution_is_the_sensor_maximum():
    assert "width: { ideal: 4096 }" in APP


def test_camera_without_zoom_support_says_so_instead_of_showing_a_dead_slider():
    assert "nie pozwala go ustawić" in APP
    assert "slider.disabled = true" in APP


def test_denied_permission_falls_back_to_the_native_button():
    """Brak zgody na aparat nie może zablokować wysyłania zdjęć."""
    assert "Aparat w aplikacji niedostępny" in APP
    assert "Użyj zwykłego przycisku" in APP


def test_stream_is_stopped_when_the_viewfinder_closes():
    """Niezwolniony strumień zostawia zapaloną diodę aparatu i zjada baterię."""
    assert "track.stop()" in APP
    assert "closeViewfinder" in APP


def test_shot_goes_through_the_ordinary_upload_path():
    """Zdjęcie z podglądu musi trafić do tej samej kolejki i tej samej
    poczekalni offline (#60) — inaczej tryb precyzyjny byłby drugą, gorszą
    drogą wysyłki."""
    shoot = APP.split("async function shoot()")[1].split("async function sendText")[0]
    assert "send([file])" in shoot


def test_capture_reports_the_resolution_so_the_spike_can_be_judged():
    """Porównanie z aparatem natywnym ma być możliwe bez zaglądania na
    serwer — to cały cel tego spike'u."""
    assert "createImageBitmap" in APP
    assert re.search(r"bitmap\.width.*bitmap\.height", APP)


def test_viewfinder_has_a_full_screen_style():
    assert ".viewfinder {" in RULES
    assert "position: fixed" in RULES.split(".viewfinder {")[1].split("}")[0]


def test_shutter_is_a_thumb_sized_target():
    """Migawka klikana jedną ręką przy module trzymanym w drugiej."""
    block = RULES.split(".vf-buttons .btn {")[1].split("}")[0]
    height = re.search(r"min-height:\s*(\d+)px", block)
    assert height and int(height.group(1)) >= 48


def test_hidden_viewfinder_is_really_hidden():
    """`display: flex` w regule autora BIJE `[hidden] { display: none }`
    z arkusza przeglądarki. Zmierzone po wdrożeniu v8: pusta nakładka na
    cały ekran przechwytywała każde dotknięcie i aplikacja przestała
    reagować. Reguła `[hidden]` musi być jawna."""
    assert ".viewfinder[hidden]" in RULES
    block = RULES.split(".viewfinder[hidden]")[1].split("}")[0]
    assert "display: none" in block
