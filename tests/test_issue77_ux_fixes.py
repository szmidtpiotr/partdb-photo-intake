"""Poprawki po pierwszym realnym użyciu zakładki „Do zatwierdzenia" (#75/#77).

Dwa zgłoszenia:

* wiersz kolejki „rozjeżdżał się" po kliknięciu ⟳ — przycisk zmieniał tekst
  (`'⟳'` → `'⟳ zlecone'`), więc kolumna `auto` w gridzie zmieniała szerokość
  i wiersz widocznie skakał;
* ✓ na karcie propozycji dawało „bardzo duży lag" — bez żadnej natychmiastowej
  reakcji, a zapis do Part-DB wykonuje deduplikację po całej liście części
  (ADR-019), co przy większym magazynie kosztuje realne sekundy.

Do tego doszła prośba o zbiorcze zlecanie rozpoznania kilku pozycjom naraz,
zamiast klikania ⟳ w każdym wierszu z osobna.
"""

import re
from pathlib import Path

PWA = Path(__file__).resolve().parent.parent / "pwa"
INDEX = (PWA / "index.html").read_text(encoding="utf-8")
APP = (PWA / "app.js").read_text(encoding="utf-8")
CSS = (PWA / "styles.css").read_text(encoding="utf-8")
# Same reguły, bez komentarzy — komentarze cytują selektory przy opisach
# błędów, a split po selektorze trafiałby w cytat zamiast w regułę.
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


# ─── Wiersz, który się nie rozjeżdża ──────────────────────────────────────


def test_recognize_button_text_never_changes():
    """Stan pokazuje kolor/obramowanie (`data-on`), nie długość napisu —
    inaczej kolumna `auto` zmienia szerokość i wiersz skacze po kliknięciu.
    Sam string „⟳ zlecone" może wystąpić w komentarzu wyjaśniającym poprawkę
    — sprawdzamy brak KONKRETNEGO wzorca przypisania, nie brak podłańcucha."""
    body = APP.split("function recognizeButton(item)")[1].split("\nfunction ")[0]
    assert "wanted ? '⟳' : '⟳ zlecone'" not in body
    assert body.count("button.textContent = '⟳';") == 1


def test_recognize_button_has_a_fixed_width():
    block = RULES.split(".recognize {")[1].split("}")[0]
    assert re.search(r"width:\s*44px", block)
    assert "min-width" not in block, "min-width dopuszcza wzrost — ma być stałe"


def test_every_row_reserves_the_same_grid_column():
    """Komórka zaznaczenia jest ZAWSZE dołączana, nawet pusta — inaczej
    wiersze różniłyby się liczbą kolumn, nie tylko stanem jednego przycisku."""
    assert "li.append(selectCell(item));" in APP
    body = APP.split("function selectCell(item)")[1].split("\nfunction ")[0]
    assert "return cell;" in body.split("if (!eligible)")[1].split("\n")[0], (
        "pozycja niekwalifikująca się ma dostać puste `.select-cell`, nie nic"
    )


# ─── Zbiorcze zlecanie rozpoznania ────────────────────────────────────────


def test_batch_bar_exists_and_starts_hidden():
    assert 'id="batch-bar" class="batch-bar" hidden' in INDEX
    assert 'id="batch-recognize"' in INDEX
    assert 'id="batch-clear"' in INDEX


def test_checkbox_selection_feeds_the_batch_bar():
    assert "recognizeSelection.add(item.id)" in APP
    assert "recognizeSelection.delete(item.id)" in APP
    assert "updateBatchBar()" in APP


def test_batch_recognize_sends_one_request_per_selected_item():
    body = APP.split("async function recognizeSelected()")[1].split(
        "\nasync function "
    )[0]
    assert "Promise.all(" in body, "N żądań po kolei byłoby wolniejsze niż równolegle"
    assert "/api/intake/${id}/recognize" in body


def test_selection_is_cleared_after_a_batch_action():
    body = APP.split("async function recognizeSelected()")[1].split(
        "\nasync function "
    )[0]
    assert "recognizeSelection.clear()" in body


def test_stale_selections_are_pruned_after_a_refresh():
    """Pozycja, która zniknęła z kolejki albo dostała zlecenie z innego
    miejsca (np. automat #75), musi wypaść z zaznaczenia — inaczej pasek
    liczyłby pozycje, których nie widać w żadnym wierszu."""
    body = APP.split("async function refresh()")[1].split("\nasync function ")[0]
    assert "recognizeSelection.delete(id)" in body
    assert "i.recognize_requested_at" in body, (
        "pruning ma sprawdzać aktualny stan zlecenia, nie tylko obecność w liście"
    )


def test_only_unflagged_pending_photos_get_a_checkbox():
    body = APP.split("function selectCell(item)")[1].split("\nfunction ")[0]
    assert "item.kind === 'photo'" in body
    assert "item.status === 'pending'" in body
    assert "!item.recognize_requested_at" in body


# ─── Reakcja przy zatwierdzaniu propozycji ────────────────────────────────


def test_approve_and_reject_disable_buttons_immediately():
    """Zanim odpowiedź wróci z serwera (zmierzone ~2-3 s przy dedupie po
    całej liście części), przycisk ma pokazać, że coś się dzieje — inaczej
    wygląda to jak zawieszona aplikacja, nie jak trwający zapis."""
    body = APP.split("async function decide(proposal, action, buttons)")[1].split(
        "\nfunction "
    )[0]
    assert "b.disabled = true" in body
    assert "'Zapisywanie…'" in body
    assert "'Odrzucanie…'" in body


def test_failed_decision_re_enables_the_card_instead_of_vanishing():
    """Niejednoznaczna deduplikacja jest najczęstszym powodem odmowy — karta
    ma zostać, żeby dało się spróbować ponownie po poprawce, a nie zniknąć
    tak, jak po sukcesie."""
    body = APP.split("async function decide(proposal, action, buttons)")[1].split(
        "\nfunction "
    )[0]
    catch_block = body.split("} catch (err) {")[1].split("return;")[0]
    assert "b.disabled = false" in catch_block
    assert "trigger.textContent = label" in catch_block


def test_buttons_carry_which_action_they_trigger():
    assert "ok.dataset.action = 'approve'" in APP
    assert "no.dataset.action = 'reject'" in APP


# ─── Pasek zbiorczy nie wisi poza zakładką „Czeka" ────────────────────────
#
# Zmierzone zgłoszenie: pasek „Zleć rozpoznanie" (`position: fixed`, dół
# ekranu) sprawdzał tylko liczbę zaznaczeń, nie aktywną zakładkę — po
# przełączeniu na „Do zatwierdzenia" zostawał widoczny i zasłaniał
# „✓ Zatwierdź" na karcie propozycji, mimo że w tej zakładce nic nie da
# się zaznaczyć (checkboksy istnieją tylko w wierszach „Czeka").


def test_batch_bar_hides_outside_the_queue_tab():
    body = APP.split("function updateBatchBar()")[1].split("\nasync function ")[0]
    assert "activeTab !== 'queue'" in body


def test_switching_tabs_hides_the_bar_immediately():
    """Bez tego pasek wisiał do następnego `refresh()`, nie od razu po
    kliknięciu zakładki."""
    body = APP.split("function setTab(tab)")[1].split("\nfunction ")[0]
    assert "updateBatchBar();" in body.split("renderActive();")[0]


def test_hidden_always_wins_over_layout_rules():
    """Zaliczone dwa razy: `.viewfinder { display: flex }` (v8) i
    `.batch-bar { display: flex }` (v10-v11) biły `[hidden]` z arkusza
    przeglądarki, więc `element.hidden = true` nie ukrywał niczego —
    pasek wisiał na wierzchu na każdej zakładce, choć kod był poprawny.
    Reguła globalna zamyka klasę błędu także dla elementów dodanych później."""
    match = re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", RULES)
    assert match, "brak globalnego `[hidden] { display: none !important }`"


def test_layout_rules_never_shadow_hidden_again():
    """Każdy selektor z własnym `display:`, który kod przełącza atrybutem
    `hidden`, jest kandydatem na ten sam błąd. Reguła globalna jest jedyną
    obroną, która działa też dla elementów dodanych później — dlatego test
    pilnuje jej istnienia, a nie listy znanych przypadków."""
    assert "bar.hidden =" in APP or "$('batch-bar').hidden =" in APP, (
        "pasek ma być przełączany atrybutem `hidden` — zmieniło się API?"
    )
    for selector in (".batch-bar {", ".viewfinder {"):
        block = RULES.split(selector)[1].split("}")[0]
        assert "display:" in block, (
            f"{selector} nie ma już `display:` — jeśli to zmiana celowa, "
            "test można uprościć"
        )
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", RULES)


# ─── Automat: tempo i widoczność błędów (#75, zgłoszenie o wolnym tempie) ────
#
# Zmierzone: 23 zlecone pozycje, po 30+ minutach zostało 19 — dużo mniej
# ukończonych, niż dawałoby 3 przebiegi crona co 10 minut po 3 pozycje.
# Trzy z tych przebiegów napisały „brak zleceń", mimo że w tej samej chwili
# w bazie czekało 19 zleconych pozycji bez żadnej propozycji — czyli sesja
# albo dostała błąd transportu MCP i potraktowała go jak pustą odpowiedź,
# albo coś inne poszło nie tak bez żadnego śladu w logu.


def test_batch_size_matches_the_hourly_schedule():
    """Przebiegi są raz na godzinę (decyzja użytkownika), więc paczka musi
    nadrobić realną zaległość w jednym podejściu. Zmierzone: ~24 s na
    pozycję, budżet przebiegu 900 s."""
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    prompt = (scripts / "recognize_requested.md").read_text(encoding="utf-8")
    script = (scripts / "recognize_requested.sh").read_text(encoding="utf-8")
    assert "limit=3" not in prompt and "limit=8" not in prompt
    assert "limit=15" in prompt
    assert "BOM_RECOGNIZE_TIMEOUT:-900" in script, (
        "15 pozycji × ~24 s nie zmieści się w 600 s z zapasem na intake_zoom"
    )


def test_empty_queue_never_wakes_a_session():
    """Zmierzone: 45 sesji zapisanych tylko po to, żeby napisać „brak
    zleceń". Stan kolejki to jedno tanie zapytanie REST — sprawdzamy to,
    co tanie, przed tym, co drogie."""
    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "recognize_requested.sh"
    ).read_text(encoding="utf-8")
    assert "pending_work()" in script
    guard = script.split('WORK="$(pending_work)"')[1].split("{")[0]
    assert 'if [ "$WORK" = "0" ]' in guard
    assert "exit 0" in guard
    # Nieczytelna kolejka (-1) NIE może uciszyć realnej zaległości.
    assert "print(-1)" in script


def test_prompt_tells_the_agent_to_report_tool_errors_distinctly():
    """Bez tego rozróżnienia „narzędzie padło" i „kolejka pusta" wygląda
    w logu identycznie — dokładnie to uniemożliwiło zdiagnozowanie
    zgłoszenia bez ręcznego wejścia na serwer."""
    prompt = (
        Path(__file__).resolve().parent.parent / "scripts" / "recognize_requested.md"
    ).read_text(encoding="utf-8")
    assert "BŁĄD:" in prompt
    assert "brak zleceń" in prompt


def test_script_logs_ground_truth_queue_depth_independent_of_the_agent():
    """Ground truth przez zwykłe REST, bez SSH/MCP — żeby dało się
    sprawdzić, czy agent powiedział prawdę, bez powtarzania tego
    dochodzenia ręcznie przy każdym kolejnym zgłoszeniu."""
    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "recognize_requested.sh"
    ).read_text(encoding="utf-8")
    assert "queue_depth()" in script
    assert "start · kolejka: $(queue_depth)" in script
    assert "koniec · kolejka: $(queue_depth)" in script
    assert "/api/intake?status=pending" in script
