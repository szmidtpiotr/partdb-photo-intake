/* Klient intake'u. Celowo bez frameworka i bez kroku budowania:
   plik jedzie na telefon takim, jakim jest w repo, a diagnoza problemu
   nie wymaga rekonstrukcji źródeł z mapy. */

const $ = (id) => document.getElementById(id);
const LOCATION_KEY = 'bom.location_hint';
// Spike #76: własny aparat i zapamiętany zoom. Wartości w localStorage, bo
// mają przeżyć zamknięcie aplikacji — cały sens jest w tym, żeby nie
// ustawiać przybliżenia przy każdym zdjęciu od nowa.
const PRECISE_KEY = 'bom.precise_camera';
const ZOOM_KEY = 'bom.camera_zoom';

// Widoczny numer wersji. Przy zgłoszeniu „nie działa" pierwsze pytanie
// brzmi „jaka wersja?", a bez tego odpowiedź wymagałaby zgadywania.
const APP_VERSION = 25;

// Dwa widoki kolejki (#69). „Czeka" to robota jeszcze do zrobienia
// (pending/processing) plus zdjęcia z poczekalni; „Archiwum" to co już
// wpadło do bazy albo odpadło (committed/rejected). Przełącznik zmienia
// tylko `activeTab` i przerysowuje z ostatniej odpowiedzi serwera — bez
// przeładowania i bez ponownego pobierania.
let activeTab = 'queue';
const QUEUE_STATES = new Set(['pending', 'processing', 'failed']);
const ARCHIVE_STATES = new Set(['committed', 'rejected']);
let lastItems = [];
let lastWaiting = [];
// Propozycje rozpoznania czekające na ✓ (#74). Trzymamy je obok pozycji,
// bo to inna robota: tam dokłada się ujęcie, tu podejmuje decyzję.
let lastProposals = [];
// Zaznaczenia do zbiorczego zlecenia rozpoznania (poprawka na #75): dobre
// jedno kliknięcie „Zleć rozpoznanie" na kilka pozycji naraz, zamiast
// ⟳ na każdej z osobna.
const recognizeSelection = new Set();

// Adresy podglądów zbudowane z blobów poczekalni. Trzymamy je, żeby
// zwolnić poprzednie przy następnym rysowaniu — bez tego sesja z setką
// zdjęć zjada pamięć telefonu obiektami, których nikt już nie ogląda.
let blobUrls = [];

// Wysyłka poczekalni chodzi pojedynczo: zdarzenie `online`, powrót do
// aplikacji i wysłanie nowego zdjęcia potrafią paść w tej samej sekundzie,
// a dwa przebiegi naraz wysłałyby to samo zdjęcie dwa razy.
let flushing = false;

function toast(message, kind = 'ok') {
  const el = $('toast');
  el.textContent = message;
  el.dataset.kind = kind;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, kind === 'bad' ? 6000 : 3000);
}

function locationHint() {
  return $('location').value.trim();
}

function timeLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString('pl-PL', today
    ? { hour: '2-digit', minute: '2-digit' }
    : { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

const STATUS_LABEL = {
  pending: 'czeka',
  processing: 'w trakcie',
  committed: 'zapisane',
  rejected: 'odrzucone',
  failed: 'nie wysłano',
};

function shotLabel(count) {
  if (count === 1) return 'zdjęcie';
  return count >= 2 && count <= 4 ? `${count} ujęcia` : `${count} ujęć`;
}

/* Przycisk „dołóż ujęcie" w wierszu pozycji. Dwa warianty, bo na telefonie
   robi się to dwiema drogami: migawką (moduł leży przed tobą) albo
   z galerii (zdjęcia są już zrobione — wtedy wolno zaznaczyć kilka naraz).
   Pole pliku siedzi w etykiecie i zostaje w układzie strony: `display:none`
   potrafi zablokować otwarcie wybieraka w Chrome na Androidzie. */
function addShot(item, fromGallery) {
  const label = document.createElement('label');
  label.className = 'add-shot';
  label.textContent = fromGallery ? '＋▤' : '＋◉';
  label.title = fromGallery
    ? 'Dodaj ujęcia z galerii do tej pozycji'
    : 'Zrób kolejne ujęcie tej samej rzeczy';
  label.setAttribute('aria-label', `${label.title} (pozycja ${item.id})`);

  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.className = 'sr-only';
  if (fromGallery) {
    input.multiple = true;
  } else {
    input.setAttribute('capture', 'environment');
  }
  input.addEventListener('change', (e) => {
    send(e.target.files, item.id);
    e.target.value = '';
  });

  label.append(input);
  return label;
}

/* „Rozpoznaj to" — jawne zlecenie, nie zgoda na przemiał kolejki.
   Automat (#75) bierze wyłącznie oznaczone pozycje, bo zdjęcie nieoznaczone
   bywa niekompletne: właśnie zamierzasz dołożyć do niego ujęcie spodu. */
function recognizeButton(item) {
  const wanted = !item.recognize_requested_at;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'recognize';
  button.dataset.on = String(!wanted);
  // Napis jest STAŁY. Wcześniej '⟳' → '⟳ zlecone' zmieniało szerokość
  // przycisku i wiersz widocznie „skakał" po kliknięciu — zmierzone
  // zgłoszenie. Stan pokazuje kolor/obramowanie (`.recognize[data-on]`),
  // nie długość tekstu.
  button.textContent = '⟳';
  button.title = wanted
    ? 'Zleć rozpoznanie tej pozycji'
    : 'Zlecone do rozpoznania — kliknij, żeby zdjąć zlecenie';
  button.setAttribute('aria-label', `${button.title} (pozycja ${item.id})`);
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/intake/${item.id}/recognize?wanted=${wanted}`, { method: 'POST' });
      if (!response.ok) throw new Error(await errorText(response));
      toast(wanted ? 'Zlecone do rozpoznania' : 'Zlecenie zdjęte');
    } catch (err) {
      toast(`Nie udało się: ${err.message || err}`, 'bad');
    }
    button.disabled = false;
    refresh();
  });
  return button;
}

/* Notatka do zdjęcia: ile sztuk, co to jest, czego nie widać (#75).
   Zdjęcie nie powie, ile tranzystorów leży w woreczku — ty to wiesz. */
function noteButton(item) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'note';
  button.dataset.on = String(Boolean(item.payload_text));
  button.textContent = '✎';
  button.title = item.payload_text
    ? `Notatka: ${item.payload_text} — kliknij, żeby zmienić`
    : 'Dopisz notatkę: ilość, nazwa, co jest w środku';
  button.setAttribute('aria-label', `${button.title} (pozycja ${item.id})`);
  button.addEventListener('click', async () => {
    const text = prompt(
      'Notatka do zdjęcia (np. „BC557, 10 szt”). Pusta = usuń notatkę.',
      item.payload_text || '');
    if (text === null) return;
    const body = new FormData();
    body.append('text', text);
    button.disabled = true;
    try {
      const response = await fetch(`/api/intake/${item.id}/note`, { method: 'POST', body });
      if (!response.ok) throw new Error(await errorText(response));
      toast(text.trim() ? 'Notatka zapisana' : 'Notatka usunięta');
    } catch (err) {
      toast(`Nie zapisano: ${err.message || err}`, 'bad');
    }
    button.disabled = false;
    refresh();
  });
  return button;
}

/* Zaznaczenie do zbiorczego zlecenia. Komórka jest ZAWSZE obecna (patrz
   `.item` w CSS) — puste, gdy pozycja nie kwalifikuje się, żeby wiersze nie
   różniły się liczbą kolumn siatki. */
function selectCell(item) {
  const cell = document.createElement('div');
  cell.className = 'select-cell';
  const eligible = item.kind === 'photo' && item.status === 'pending'
    && item.id && !item.recognize_requested_at;
  if (!eligible) return cell;

  const input = document.createElement('input');
  input.type = 'checkbox';
  input.checked = recognizeSelection.has(item.id);
  input.setAttribute('aria-label',
    `Zaznacz pozycję ${item.id} do zbiorczego zlecenia rozpoznania`);
  input.addEventListener('change', () => {
    if (input.checked) recognizeSelection.add(item.id);
    else recognizeSelection.delete(item.id);
    updateBatchBar();
  });
  cell.append(input);
  return cell;
}

function updateBatchBar() {
  const bar = $('batch-bar');
  const n = recognizeSelection.size;
  // Checkboksy istnieją tylko w wierszach zakładki „Czeka" — w „Do
  // zatwierdzenia" i „Archiwum" nic nie da się zaznaczyć. Zmierzone
  // zgłoszenie: pasek (`position: fixed`, dół ekranu) zostawał widoczny
  // po przełączeniu zakładki i zasłaniał „✓ Zatwierdź" na karcie
  // propozycji, bo sprawdzał tylko liczbę zaznaczeń, nie aktywną zakładkę.
  bar.hidden = n === 0 || activeTab !== 'queue';
  if (bar.hidden) return;
  $('batch-count').textContent =
    `${n} ${n === 1 ? 'pozycja' : (n <= 4 ? 'pozycje' : 'pozycji')} do zlecenia`;
}

async function recognizeSelected() {
  const ids = [...recognizeSelection];
  if (!ids.length) return;
  $('batch-recognize').disabled = true;
  try {
    const results = await Promise.all(
      ids.map((id) => fetch(`/api/intake/${id}/recognize`, { method: 'POST' })));
    const failed = results.filter((r) => !r.ok).length;
    toast(
      failed
        ? `Zlecono ${ids.length - failed} z ${ids.length} — ${failed} nie powiodło się`
        : `Zlecono rozpoznanie: ${ids.length}`,
      failed ? 'bad' : 'ok',
    );
  } catch (err) {
    toast(`Nie udało się zlecić: ${err.message || err}`, 'bad');
  }
  recognizeSelection.clear();
  $('batch-recognize').disabled = false;
  updateBatchBar();
  refresh();
}

/* Powód odmowy z serwera, nie sam kod stanu: „nazwa nie może zostać pusta"
   mówi użytkownikowi, co poprawić, a „HTTP 409" nie mówi nic. */
async function errorText(response) {
  try {
    const data = await response.json();
    return data.error || data.detail || `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

function itemNode(item) {
  const li = document.createElement('li');
  li.className = 'item';
  li.append(selectCell(item));

  const src = item.photo_url || item.localUrl;
  if (src) {
    const img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.src = src;
    li.append(img);
  } else {
    // Pozycja podyktowana nie ma miniatury. Pusta ramka wyglądałaby na
    // zdjęcie, które się nie wczytało — stąd jawny znacznik.
    const glyph = document.createElement('div');
    glyph.className = 'glyph';
    glyph.textContent = 'opis';
    li.append(glyph);
  }

  const meta = document.createElement('div');
  meta.className = 'meta';
  const title = document.createElement('div');
  title.className = 'title';
  title.textContent = item.kind === 'photo'
    ? (item.location_hint || 'zdjęcie bez lokalizacji')
    : (item.payload_text || 'opis');
  const shots = item.shots ? item.shots.length : 0;
  const sub = document.createElement('div');
  sub.className = 'sub';
  const waiting = item.proposals_pending || 0;
  sub.textContent = [
    item.kind !== 'photo' ? 'opis' : shotLabel(shots || 1),
    timeLabel(item.created_at),
    item.kind !== 'photo' && item.location_hint ? item.location_hint : null,
    item.kind === 'photo' && item.payload_text ? `✎ ${item.payload_text}` : null,
    // Rozpoznane, ale jeszcze nie w bazie — inaczej wiersz wyglądałby jak
    // zdjęcie czekające na rozpoznanie i prosiłby o nie drugi raz.
    waiting ? `${waiting} do zatwierdzenia` : null,
    item.recognize_requested_at && !waiting ? 'zlecone do rozpoznania' : null,
    item.note || null,
  ].filter(Boolean).join(' · ');
  meta.append(title, sub);

  // Co z tej pozycji powstało (#69): nazwa części + odnośnik do rekordu
  // w Part-DB. Nazwa jest zawsze; link tylko dla nowo utworzonych części
  // (scalone w istniejącą nie mają własnego numeru rekordu z tego zapisu).
  if (item.status === 'committed' && item.created_parts && item.created_parts.length) {
    const parts = document.createElement('div');
    parts.className = 'parts';
    for (const record of item.created_parts) {
      let node;
      if (record.url) {
        node = document.createElement('a');
        node.href = record.url;
        node.target = '_blank';
        node.rel = 'noopener';
      } else {
        node = document.createElement('span');
      }
      node.className = 'part';
      node.textContent = record.name;
      parts.append(node);
    }
    meta.append(parts);
  }

  li.append(meta);

  const actions = document.createElement('div');
  actions.className = 'actions-cell';

  // Kolejne ujęcie tej samej rzeczy. Moduł trzeba czasem sfotografować
  // z wierzchu, od spodu i z bliska na sitodruk — a to nadal jeden
  // element, nie trzy pozycje do rozpoznania.
  if (item.kind === 'photo' && item.status === 'pending' && item.id) {
    actions.append(
      addShot(item, false), addShot(item, true), noteButton(item), recognizeButton(item));
  }

  if (item.status === 'failed') {
    const button = document.createElement('button');
    button.className = 'retry';
    button.type = 'button';
    button.textContent = 'Ponów';
    button.addEventListener('click', () => retry(item.localId));
    actions.append(button);
  } else {
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.dataset.status = item.status;
    tag.textContent = STATUS_LABEL[item.status] || item.status;
    actions.append(tag);
  }

  // Zatwierdzonych nie kasujemy: wskazują na części zapisane w magazynie
  // i są jedynym śladem pozwalającym ten zapis cofnąć.
  if (item.status !== 'committed') {
    const del = document.createElement('button');
    del.className = 'delete';
    del.type = 'button';
    del.title = 'Usuń';
    del.setAttribute('aria-label', `Usuń pozycję ${item.id ?? ''}`);
    del.textContent = '✕';
    del.addEventListener('click', () => remove(item));
    actions.append(del);
  }

  li.append(actions);
  return li;
}

async function remove(item) {
  // Zdjęcie z poczekalni nigdy nie dotarło na serwer — nie ma czego kasować
  // po drugiej stronie, ale z IndexedDB musi zniknąć, inaczej wróci przy
  // następnym zasięgu jako duch.
  if (item.status === 'failed') {
    if (!confirm('Wyrzucić to zdjęcie? Nigdy nie zostało wysłane.')) return;
    await Outbox.remove(item.localId);
    refresh();
    return;
  }

  const co = item.kind === 'photo' ? 'to zdjęcie' : 'ten opis';
  if (!confirm(`Usunąć ${co} z kolejki? Tego nie da się cofnąć.`)) return;

  try {
    const response = await fetch(`/api/intake/${item.id}`, { method: 'DELETE' });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        detail = data.error || data.detail || detail;
      } catch { /* bez JSON-a zostaje kod stanu */ }
      throw new Error(detail);
    }
    toast('Usunięte');
  } catch (err) {
    toast(`Nie usunięto: ${err.message || err}`, 'bad');
  }
  refresh();
}

/* ─── Propozycje do zatwierdzenia (#74, ADR-021) ──────────────────────────
   Karta pokazuje WYCINEK zdjęcia z tą jedną płytką (#72). Przy pięciu
   propozycjach z jednego kadru miniatury z całym blatem byłyby
   nierozróżnialne, a to jedyna rzecz, po której poznajesz, co zatwierdzasz. */

function field(proposal, key, label, options = {}) {
  const wrap = document.createElement('label');
  wrap.className = 'card-field';
  const span = document.createElement('span');
  span.textContent = label;
  const input = document.createElement('input');
  input.type = options.numeric ? 'number' : 'text';
  if (options.numeric) {
    input.inputMode = 'decimal';
    input.min = '0';
    input.step = 'any';
  }
  input.value = proposal.payload[key] ?? '';
  input.placeholder = options.placeholder || '';

  // Zapis na `change`, nie na każde uderzenie w klawiaturę: telefon wysyłałby
  // wtedy żądanie na literę, a poprawka nazwy to kilkanaście liter.
  input.addEventListener('change', async () => {
    const value = options.numeric ? Number(input.value) : input.value.trim();
    try {
      const response = await fetch(`/api/proposals/${proposal.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value }),
      });
      if (!response.ok) throw new Error(await errorText(response));
      const updated = await response.json();
      // Podmieniamy w pamięci, bez przerysowania: przerysowanie zabrałoby
      // kursor z pola, w którym użytkownik właśnie coś pisze.
      Object.assign(proposal, updated);
      input.value = updated.payload[key] ?? '';
      toast('Poprawione');
    } catch (err) {
      toast(`Nie zapisano: ${err.message || err}`, 'bad');
      input.value = proposal.payload[key] ?? '';
    }
  });

  wrap.append(span, input);
  return wrap;
}

function verdictNode(proposal) {
  const line = document.createElement('div');
  line.className = 'verdict';
  line.dataset.action = proposal.action;
  if (proposal.action === 'merge' && proposal.existing) {
    line.textContent = `dołoży stan do: ${proposal.existing.name}`;
  } else {
    line.textContent = 'utworzy nową część';
  }
  const extras = [
    proposal.new_category ? `nowa kategoria: ${proposal.new_category}` : null,
    proposal.location ? proposal.location : 'bez pojemnika',
    proposal.payload.needs_review ? 'do sprawdzenia' : null,
  ].filter(Boolean);
  const sub = document.createElement('span');
  sub.className = 'verdict-sub';
  sub.textContent = extras.join(' · ');
  line.append(sub);
  return line;
}

async function decide(proposal, action, buttons) {
  // Zapis do Part-DB bierze udział w deduplikacji (ADR-019) i bywa wolny
  // przy większym magazynie — zmierzone ~2-3 s na pierwszym zatwierdzeniu
  // w sesji, bo dedup czyta CAŁĄ listę części. Bez natychmiastowej reakcji
  // wygląda to jak zawieszona aplikacja, nie jak trwający zapis.
  const trigger = buttons.find((b) => b.dataset.action === action);
  const label = trigger ? trigger.textContent : '';
  for (const b of buttons) b.disabled = true;
  if (trigger) trigger.textContent = action === 'approve' ? 'Zapisywanie…' : 'Odrzucanie…';

  const url = `/api/proposals/${proposal.id}/${action}`;
  try {
    const response = await fetch(url, { method: 'POST' });
    if (!response.ok) throw new Error(await errorText(response));
    toast(action === 'approve' ? 'Zapisane w magazynie' : 'Odrzucone');
  } catch (err) {
    // Najczęstszy powód odmowy przy zatwierdzaniu to niejednoznaczna
    // deduplikacja — decyzja należy wtedy do agenta, nie do ekranu.
    toast(`${action === 'approve' ? 'Nie zapisano' : 'Nie odrzucono'}: `
      + `${err.message || err}`, 'bad');
    // Karta zostaje: użytkownik ma spróbować ponownie albo odrzucić,
    // a znikający przycisk po błędzie wyglądałby jak drugi błąd.
    for (const b of buttons) b.disabled = false;
    if (trigger) trigger.textContent = label;
    return;
  }
  refresh();
}

function proposalNode(proposal) {
  const li = document.createElement('li');
  li.className = 'card';

  if (proposal.photo_url) {
    const img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.src = proposal.photo_url;
    li.append(img);
  }

  const body = document.createElement('div');
  body.className = 'card-body';
  body.append(
    field(proposal, 'name', 'Co to jest'),
    field(proposal, 'amount', 'Ile', { numeric: true }),
    field(proposal, 'category', 'Kategoria'),
    field(proposal, 'location', 'Gdzie leży', { placeholder: 'np. A/A1' }),
    verdictNode(proposal),
  );

  if (proposal.payload.note) {
    const note = document.createElement('p');
    note.className = 'card-note';
    note.textContent = proposal.payload.note;
    body.append(note);
  }

  // Opis trafia do Part-DB razem z częścią i to po nim działa wyszukiwarka
  // (pełna lista wartości zestawu + linia „Szukaj:"). Bywa długi, więc
  // zwinięty — ma się dać sprawdzić przed zatwierdzeniem, nie zasłaniać karty.
  if (proposal.payload.description) {
    const details = document.createElement('details');
    details.className = 'card-desc';
    const summary = document.createElement('summary');
    summary.textContent = 'Opis i słowa do wyszukiwania';
    const text = document.createElement('p');
    text.textContent = proposal.payload.description;
    details.append(summary, text);
    body.append(details);
  }

  const buttons = document.createElement('div');
  buttons.className = 'card-actions';
  const ok = document.createElement('button');
  ok.type = 'button';
  ok.className = 'btn btn-primary';
  ok.dataset.action = 'approve';
  ok.textContent = '✓ Zatwierdź';
  const no = document.createElement('button');
  no.type = 'button';
  no.className = 'btn btn-quiet';
  no.dataset.action = 'reject';
  no.textContent = '✕ Odrzuć';
  ok.addEventListener('click', () => decide(proposal, 'approve', [ok, no]));
  no.addEventListener('click', () => {
    if (confirm(`Odrzucić „${proposal.payload.name}"? Zdjęcie zostaje w kolejce.`)) {
      decide(proposal, 'reject', [ok, no]);
    }
  });
  buttons.append(ok, no);
  body.append(buttons);

  li.append(body);
  return li;
}

/* Nagłówek grupy: jedno zdjęcie potrafi dać kilka propozycji (#72), a wtedy
   klikanie ✓ pięć razy jest pracą bez treści. „Zatwierdź wszystkie" zapisuje
   je po kolei, więc deduplikacja każdej kolejnej widzi poprzednią. */
function groupHeader(itemId, proposals) {
  const li = document.createElement('li');
  li.className = 'group';

  const label = document.createElement('span');
  const n = proposals.length;
  label.textContent = `pozycja #${itemId} · ${n} ${
    n === 1 ? 'propozycja' : (n <= 4 ? 'propozycje' : 'propozycji')}`;
  li.append(label);

  if (n > 1) {
    const all = document.createElement('button');
    all.type = 'button';
    all.className = 'btn btn-quiet';
    all.textContent = `✓ wszystkie (${n})`;
    all.addEventListener('click', async () => {
      all.disabled = true;
      try {
        const response = await fetch(`/api/intake/${itemId}/approve`,
          { method: 'POST' });
        if (!response.ok) throw new Error(await errorText(response));
        const data = await response.json();
        toast(`Zapisane: ${data.approved.length}`);
      } catch (err) {
        toast(`Nie zapisano wszystkich: ${err.message || err}`, 'bad');
      }
      refresh();
    });
    li.append(all);
  }
  return li;
}

function renderProposals() {
  const list = $('items');
  const groups = new Map();
  for (const proposal of lastProposals) {
    if (!groups.has(proposal.item_id)) groups.set(proposal.item_id, []);
    groups.get(proposal.item_id).push(proposal);
  }
  const nodes = [];
  for (const [itemId, proposals] of groups) {
    nodes.push(groupHeader(itemId, proposals), ...proposals.map(proposalNode));
  }
  list.replaceChildren(...nodes);
}

let lastCounts = {};

function render(items) {
  const list = $('items');
  list.replaceChildren(...items.map(itemNode));
}

/* Rysuje aktywną zakładkę z ostatniej odpowiedzi serwera. Odznaki liczą
   ze `counts` (globalne, niezależne od limitu pobrania), więc pokazują cały
   stan bazy, nie tylko wczytaną stronę. */
function renderActive() {
  const c = lastCounts || {};
  // Pozycja z propozycjami nie jest już „do zrobienia" — czeka na decyzję
  // w swojej zakładce. Zostawienie jej w „Czeka" prosiłoby o rozpoznanie
  // rzeczy właśnie rozpoznanej.
  const awaiting = new Set(lastProposals.map((proposal) => proposal.item_id));
  const inQueue = lastItems.filter(
    (i) => QUEUE_STATES.has(i.status) && !awaiting.has(i.id));
  const inArchive = lastItems.filter((i) => ARCHIVE_STATES.has(i.status));

  $('badge-queue').textContent =
    String(Math.max(0, (c.pending || 0) - awaiting.size)
      + (c.processing || 0) + lastWaiting.length);
  $('badge-review').textContent = String(lastProposals.length);
  $('badge-archive').textContent = String((c.committed || 0) + (c.rejected || 0));

  if (activeTab === 'review') {
    renderProposals();
    $('empty').hidden = lastProposals.length > 0;
    $('empty').textContent =
      'Nic nie czeka na zatwierdzenie. Zleć rozpoznanie przyciskiem ⟳ '
      + 'przy zdjęciu w zakładce „Czeka".';
    $('queue-help').hidden = true;
    return;
  }

  const visible = activeTab === 'queue' ? [...lastWaiting, ...inQueue] : inArchive;
  render(visible);

  $('empty').hidden = visible.length > 0;
  $('empty').textContent = activeTab === 'queue'
    ? 'Nic nie czeka. Zrób pierwsze zdjęcie.'
    : 'Archiwum jest puste — nic jeszcze nie wczytano do bazy.';
  $('queue-help').hidden = activeTab !== 'queue';
}

function setTab(tab) {
  activeTab = tab;
  for (const [name, id] of [['queue', 'tab-queue'], ['review', 'tab-review'],
                            ['archive', 'tab-archive']]) {
    $(id).setAttribute('aria-selected', String(tab === name));
  }
  // Pasek chowa się natychmiast przy zmianie zakładki, nie tylko po
  // następnym `refresh()` — zaznaczenie zostaje (wracasz do „Czeka" i
  // kontynuujesz), ale pasek nie ma prawa wisieć nad kartami propozycji.
  updateBatchBar();
  renderActive();
}

/* Wpis poczekalni udaje pozycję kolejki, bo dla użytkownika to jest to samo:
   zdjęcie, które zrobił i które czeka. Różni się tym, że jeszcze nie dotarło
   na serwer — i to musi być widać. */
function waitingNode(entry) {
  const url = URL.createObjectURL(entry.blob);
  blobUrls.push(url);
  return {
    localId: entry.id,
    status: 'failed',
    kind: 'photo',
    created_at: entry.createdAt,
    location_hint: entry.locationHint || null,
    note: entry.parentId
      ? `ujęcie do pozycji ${entry.parentId} · ${entry.note || 'czeka na zasięg'}`
      : entry.note || 'czeka na zasięg',
    localUrl: url,
  };
}

async function refresh() {
  for (const url of blobUrls) URL.revokeObjectURL(url);
  blobUrls = [];

  lastWaiting = [];
  try {
    lastWaiting = (await Outbox.all()).map(waitingNode);
  } catch (err) {
    // Brak IndexedDB (tryb prywatny, wyczyszczone dane witryny) nie może
    // wyłączyć aplikacji — wysyłanie na żywo działa dalej.
    console.warn('Poczekalnia niedostępna', err);
  }

  const waitingLabel = lastWaiting.length ? ` · nie wysłano ${lastWaiting.length}` : '';
  try {
    // Limit hojny, bo archiwum ma przeżyć zamknięcie karty i pokazać, co już
    // w bazie — dane z serwera, nie z pamięci przeglądarki (#69).
    const response = await fetch('/api/intake?limit=200', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    lastItems = data.items;
    lastCounts = data.counts || {};
    // Osobne żądanie, bo propozycje to inna lista, nie pole pozycji: jedno
    // zdjęcie daje ich kilka, a każda ma własny wycinek i własną decyzję.
    try {
      const cards = await fetch('/api/proposals?status=pending',
        { cache: 'no-store' });
      lastProposals = cards.ok ? (await cards.json()).proposals : [];
    } catch {
      lastProposals = [];
    }
    const c = lastCounts;
    $('counts').textContent =
      `czeka ${Math.max(0, (c.pending || 0) - (c.awaiting_review || 0))} · `
      + `do zatwierdzenia ${lastProposals.length} · `
      + `zapisane ${c.committed || 0}${waitingLabel}`;
    $('status').dataset.state = 'ok';
    $('status').textContent = `połączono · v${APP_VERSION}`;
    // Pozycje, które zniknęły z kolejki albo dostały zlecenie z innego
    // miejsca (np. automat #75 już je podjął), muszą wypaść z zaznaczenia —
    // inaczej pasek liczyłby pozycje, których nie widać w żadnym wierszu.
    const eligible = new Set(
      lastItems
        .filter((i) => i.kind === 'photo' && i.status === 'pending'
          && !i.recognize_requested_at)
        .map((i) => i.id),
    );
    for (const id of recognizeSelection) {
      if (!eligible.has(id)) recognizeSelection.delete(id);
    }
  } catch (err) {
    lastItems = [];
    lastCounts = {};
    lastProposals = [];
    $('counts').textContent = lastWaiting.length
      ? `nie wysłano ${lastWaiting.length}`
      : '';
    $('status').dataset.state = 'bad';
    $('status').textContent = `brak połączenia · v${APP_VERSION}`;
  }
  updateBatchBar();
  renderActive();
}

/* Rozróżnienie, na którym stoi cała poczekalnia: brak zasięgu mija,
   niewłaściwy format nie mija nigdy. Ponawianie w kółko czegoś, co Part-DB
   i tak odrzuci, wyglądałoby jak awaria, a nie jak odmowa. */
class UploadError extends Error {
  constructor(message, permanent) {
    super(message);
    this.permanent = Boolean(permanent);
  }
}

async function upload(file, parentId, name) {
  const body = new FormData();
  body.append('photo', file, name || file.name || 'zdjecie.jpg');
  // Ujęcie dokładane do istniejącej pozycji dziedziczy jej lokalizację —
  // opisuje tę samą rzecz, więc pole z nagłówka nie ma tu nic do rzeczy.
  if (parentId) {
    body.append('parent_id', String(parentId));
  } else {
    const hint = locationHint();
    if (hint) body.append('location_hint', hint);
  }

  let response;
  try {
    response = await fetch('/api/intake/photo', { method: 'POST', body });
  } catch (err) {
    // Brak zasięgu, uśpiony router, przerwany tunel — to mija samo.
    throw new UploadError(err.message || 'brak połączenia', false);
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || data.error || detail;
    } catch { /* odpowiedź bez JSON-a — zostaje kod stanu */ }
    // 4xx mówi o samym zdjęciu albo żądaniu (zły format, za duże, pozycja
    // zniknęła) i ponawianie tego nie naprawi. 408 i 429 są wyjątkiem,
    // bo serwer wprost prosi o później.
    const permanent = response.status >= 400 && response.status < 500
      && response.status !== 408 && response.status !== 429;
    throw new UploadError(detail, permanent);
  }
  return response.json();
}

async function send(files, parentId) {
  const list = [...(files || [])];

  // Cisza przy pustym wyborze była najgorszym zachowaniem tej aplikacji:
  // wybierak potrafi wrócić bez pliku (anulowanie, ale też zdjęcie leżące
  // wyłącznie w chmurze), a użytkownik widział wtedy dokładnie nic.
  if (!list.length) {
    toast('Nie wybrano żadnego zdjęcia', 'bad');
    return;
  }

  let sent = 0;
  let parked = 0;

  for (const file of list) {
    try {
      if (!file.size) {
        throw new UploadError(
          'plik jest pusty — jeśli zdjęcie jest tylko w Zdjęciach Google, '
          + 'pobierz je najpierw na telefon',
          true
        );
      }
      await upload(file, parentId);
      sent += 1;
    } catch (err) {
      // Zdjęcia nie gubimy nigdy — nawet odrzuconego. Ląduje w poczekalni
      // z powodem, a użytkownik decyduje: ponowić czy wyrzucić.
      try {
        await Outbox.add({
          blob: file,
          name: file.name,
          locationHint: parentId ? '' : locationHint(),
          parentId: parentId || null,
          note: String(err.message || err),
          blocked: err.permanent,
        });
        parked += 1;
        toast(
          err.permanent
            ? `Odrzucone: ${err.message || err}`
            : `Brak zasięgu — zdjęcie czeka w telefonie (${err.message || err})`,
          'bad'
        );
      } catch (storeError) {
        // Prywatne okno albo pełny dysk telefonu. Milczenie byłoby tu
        // najgorsze: użytkownik odszedłby przekonany, że zdjęcie czeka.
        toast(
          `NIE ZAPISANO: ${err.message || err}. Poczekalnia niedostępna `
          + `(${storeError.message || storeError}) — zrób zdjęcie ponownie `
          + 'przy zasięgu.',
          'bad'
        );
      }
    }
  }

  if (parked && navigator.vibrate) navigator.vibrate([30, 60, 30]);

  if (sent) {
    if (parentId) {
      toast(sent === 1
        ? `Ujęcie dołożone do pozycji ${parentId}`
        : `${sent} ujęć dołożonych do pozycji ${parentId}`);
    } else {
      toast(sent === 1 ? 'Zdjęcie w kolejce' : `${sent} zdjęcia w kolejce`);
    }
    if (navigator.vibrate) navigator.vibrate(30);
    // Skoro to zdjęcie poszło, zasięg jest — zaległe też mają szansę.
    flush();
  }
  refresh();
}

/* Jedno zdjęcie z poczekalni. Zwraca, co się z nim stało — flush liczy
   na tym, a przycisk „Ponów" pokazuje wynik użytkownikowi. */
async function sendWaiting(entry) {
  try {
    await upload(entry.blob, entry.parentId, entry.name);
    await Outbox.remove(entry.id);
    return 'sent';
  } catch (err) {
    // Pozycja główna mogła zostać zatwierdzona albo skasowana, zanim wrócił
    // zasięg. Ujęcie idzie wtedy jako własna pozycja: osobny wiersz do
    // scalenia ręką jest lepszy niż zdjęcie wyrzucone do kosza.
    if (err.permanent && entry.parentId) {
      try {
        await upload(entry.blob, null, entry.name);
        await Outbox.remove(entry.id);
        return 'detached';
      } catch (again) {
        err = again;
      }
    }
    await Outbox.update(entry.id, {
      note: err.permanent ? `odrzucone: ${err.message}` : String(err.message || err),
      blocked: err.permanent,
    });
    return err.permanent ? 'blocked' : 'waiting';
  }
}

/* Wysyłka poczekalni. Chodzi przy starcie aplikacji, po powrocie zasięgu
   i po powrocie do karty — świadomie bez Background Sync, którego iOS nie
   ma, więc jako jedyny mechanizm byłby obietnicą bez pokrycia. */
async function flush() {
  if (flushing || !navigator.onLine) return 0;
  flushing = true;

  let sent = 0;
  let detached = 0;
  try {
    for (const entry of await Outbox.all()) {
      if (entry.blocked) continue;
      const result = await sendWaiting(entry);
      if (result === 'sent') sent += 1;
      if (result === 'detached') { sent += 1; detached += 1; }
      // Pierwsza porażka z braku zasięgu kończy przebieg: kolejne zdjęcia
      // padną tak samo, a każda próba to sekundy czekania na telefonie.
      if (result === 'waiting') break;
    }
  } catch (err) {
    console.warn('Poczekalnia: wysyłka nie doszła do skutku', err);
  } finally {
    flushing = false;
  }

  if (sent) {
    toast(sent === 1 ? 'Zaległe zdjęcie wysłane' : `Wysłano ${sent} zaległych zdjęć`);
    if (detached) {
      toast(
        `${detached} ujęć trafiło jako osobne pozycje — ich pozycja `
        + 'w międzyczasie zniknęła',
        'bad'
      );
    }
    refresh();
  }
  return sent;
}

async function retry(localId) {
  const entries = await Outbox.all();
  const entry = entries.find((row) => row.id === localId);
  if (!entry) return;

  const result = await sendWaiting(entry);
  if (result === 'sent') toast('Wysłane');
  else if (result === 'detached') toast('Wysłane jako osobna pozycja');
  else toast('Nadal nie idzie — zdjęcie czeka dalej', 'bad');
  refresh();
}

/* ─── Aparat w aplikacji (spike #76) ───────────────────────────────────────

   Natywna aplikacja aparatu daje lepsze zdjęcie (pełny pipeline obliczeniowy
   telefonu), ale startuje z zoomem 1× i nie ma API, żeby jej podać stan
   początkowy. Tutaj obraz jest nasz, więc zoom da się zapamiętać — cena to
   surowa klatka z sensora zamiast przetworzonej.

   Który sposób zostaje domyślny, rozstrzyga jedno porównanie na drobnym
   sitodruku, nie przekonanie. Dlatego to przełącznik. */

let stream = null;
let videoTrack = null;

function preciseMode() {
  return localStorage.getItem(PRECISE_KEY) === '1';
}

async function openViewfinder() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: 'environment' },
        // Prosimy o maksimum, jakie sprzęt da — bez tego Chrome wybiera
        // rozdzielczość podglądu, a wtedy sitodruk ginie już na starcie.
        width: { ideal: 4096 },
        height: { ideal: 3072 },
      },
      audio: false,
    });
  } catch (err) {
    toast(`Aparat w aplikacji niedostępny: ${err.message || err}. `
      + 'Użyj zwykłego przycisku.', 'bad');
    return false;
  }

  videoTrack = stream.getVideoTracks()[0];
  $('preview').srcObject = stream;
  $('viewfinder').hidden = false;

  const caps = videoTrack.getCapabilities ? videoTrack.getCapabilities() : {};
  const slider = $('zoom');
  if (caps.zoom) {
    slider.min = caps.zoom.min;
    slider.max = caps.zoom.max;
    slider.step = caps.zoom.step || 0.1;
    slider.disabled = false;
    const saved = Number(localStorage.getItem(ZOOM_KEY) || caps.zoom.min);
    const zoom = Math.min(Math.max(saved, caps.zoom.min), caps.zoom.max);
    slider.value = zoom;
    await applyZoom(zoom);
  } else {
    // Zoom bywa nieobsługiwany (starszy Android, kamera USB). Wtedy tryb
    // precyzyjny nie ma czego zapamiętać i trzeba to powiedzieć wprost,
    // zamiast pokazywać suwak, który nic nie robi.
    slider.disabled = true;
    $('zoom-label').textContent = 'zoom: ten aparat nie pozwala go ustawić';
  }
  return true;
}

async function applyZoom(zoom) {
  if (!videoTrack) return;
  try {
    await videoTrack.applyConstraints({ advanced: [{ zoom }] });
    localStorage.setItem(ZOOM_KEY, String(zoom));
    $('zoom-label').textContent = `zoom ${Number(zoom).toFixed(1)}×`;
  } catch (err) {
    $('zoom-label').textContent = 'zoom: nie przyjęty przez aparat';
    console.warn('applyConstraints(zoom) odrzucone', err);
  }
}

function closeViewfinder() {
  if (stream) for (const track of stream.getTracks()) track.stop();
  stream = null;
  videoTrack = null;
  $('preview').srcObject = null;
  $('viewfinder').hidden = true;
}

/* Zdjęcie, nie zrzut podglądu. `ImageCapture.takePhoto` daje pełną
   rozdzielczość sensora tam, gdzie jest obsługiwane; klatka z <video> ma
   tylko tyle pikseli, ile ma podgląd, a to jest dokładnie ta różnica,
   przez którą sitodruk staje się nieczytelny. */
async function shoot() {
  if (!videoTrack) return;
  let blob = null;

  if (window.ImageCapture) {
    try {
      const capture = new ImageCapture(videoTrack);
      const caps = await capture.getPhotoCapabilities();
      blob = await capture.takePhoto({
        imageWidth: caps.imageWidth ? caps.imageWidth.max : undefined,
        imageHeight: caps.imageHeight ? caps.imageHeight.max : undefined,
      });
    } catch (err) {
      console.warn('takePhoto nie wyszło, zostaje klatka podglądu', err);
    }
  }

  if (!blob) {
    const video = $('preview');
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, 'image/jpeg', 0.92));
  }

  if (!blob) { toast('Nie udało się zrobić zdjęcia', 'bad'); return; }

  // Rozmiar w komunikacie jest po to, żeby porównanie z aparatem natywnym
  // dało się zrobić bez zaglądania na serwer — to cały cel spike'u (#76).
  const bitmap = await createImageBitmap(blob).catch(() => null);
  const size = bitmap ? `${bitmap.width}×${bitmap.height}` : 'nieznany rozmiar';
  toast(`Zdjęcie ${size} · ${Math.round(blob.size / 1024)} kB`);

  const file = new File([blob], 'precyzyjne.jpg', { type: 'image/jpeg' });
  closeViewfinder();
  send([file]);
}

async function sendText() {
  const text = $('text').value.trim();
  if (!text) { toast('Puste pole', 'bad'); return; }
  const body = new FormData();
  body.append('text', text);
  const hint = locationHint();
  if (hint) body.append('location_hint', hint);

  try {
    const response = await fetch('/api/intake/voice', { method: 'POST', body });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    $('text').value = '';
    toast('Opis w kolejce');
  } catch (err) {
    toast(`Nie wysłano: ${err.message || err}`, 'bad');
  }
  refresh();
}

/* Powrót z udostępniania (#65). Service worker odłożył zdjęcia do
   poczekalni i przekierował tutaj z podsumowaniem w adresie — aplikacja
   ma je pokazać i od razu wysłać. Adres czyścimy, żeby odświeżenie
   strony nie powtarzało komunikatu o czymś, co już się stało. */
function handleShare() {
  const params = new URLSearchParams(location.search);
  const shared = Number(params.get('udostepniono') || 0);
  const text = params.get('tekst');
  const error = params.get('blad');
  if (!params.has('udostepniono') && !text && !error) return;

  if (shared) {
    toast(shared === 1
      ? 'Udostępnione zdjęcie w kolejce'
      : `${shared} udostępnionych zdjęć w kolejce`);
  }
  if (text) {
    // Sam tekst albo adres ze sklepu. Wchodzi do pola dyktowania —
    // wysyła go użytkownik, bo tylko on wie, czego dotyczy.
    const field = $('text');
    field.value = field.value ? `${field.value} ${text}` : text;
    field.closest('details').open = true;
    toast('Udostępniony tekst wpadł do pola opisu — uzupełnij i wyślij');
  }
  if (error) toast(`Udostępnianie: ${error}`, 'bad');

  history.replaceState(null, '', '/dodaj');
}

function init() {
  const saved = localStorage.getItem(LOCATION_KEY);
  if (saved) $('location').value = saved;
  $('location').addEventListener('change', (e) =>
    localStorage.setItem(LOCATION_KEY, e.target.value.trim()));

  // Reset wartości pozwala wysłać dwa razy to samo ujęcie — inaczej
  // 'change' nie odpali, bo wartość się nie zmieniła.
  for (const id of ['camera', 'gallery']) {
    $(id).addEventListener('change', (e) => {
      send(e.target.files);
      e.target.value = '';
    });
  }

  // Tryb precyzyjny przechwytuje kliknięcie zanim otworzy się natywny
  // wybierak — pole pliku siedzi w etykiecie, więc jedyny moment na to
  // jest w fazie przechwytywania zdarzenia.
  $('precise').checked = preciseMode();
  $('precise').addEventListener('change', (e) => {
    localStorage.setItem(PRECISE_KEY, e.target.checked ? '1' : '0');
    toast(e.target.checked
      ? 'Aparat w aplikacji — zoom zostanie zapamiętany'
      : 'Wracamy do aparatu telefonu');
  });
  $('camera').closest('label').addEventListener('click', (event) => {
    if (!preciseMode()) return;
    event.preventDefault();
    openViewfinder();
  }, true);

  $('zoom').addEventListener('input', (e) => applyZoom(Number(e.target.value)));
  $('vf-shoot').addEventListener('click', shoot);
  $('vf-cancel').addEventListener('click', closeViewfinder);

  $('send-text').addEventListener('click', sendText);

  // Przełączanie widoków bez przeładowania (#69) — samo przerysowanie
  // z danych, które już są w pamięci po ostatnim `refresh`.
  $('tab-queue').addEventListener('click', () => setTab('queue'));
  $('tab-review').addEventListener('click', () => setTab('review'));
  $('tab-archive').addEventListener('click', () => setTab('archive'));

  $('batch-recognize').addEventListener('click', recognizeSelected);
  $('batch-clear').addEventListener('click', () => {
    recognizeSelection.clear();
    updateBatchBar();
    renderActive();
  });

  // Powrót zasięgu ma wysłać zaległości sam. Warunek zadania brzmiał:
  // tryb samolotowy, zdjęcie, zamknięcie aplikacji, sieć z powrotem —
  // i zdjęcie w kolejce bez ani jednego dotknięcia ekranu.
  window.addEventListener('online', () => flush().then(refresh));
  window.addEventListener('offline', refresh);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) flush().then(refresh);
  });

  handleShare();
  refresh();
  flush();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Brak service workera oznacza tylko brak instalacji na ekranie
      // głównym i brak trybu offline — wysyłanie działa dalej.
    });
  }
}

init();
