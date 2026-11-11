/* Warsztat — przeglądanie magazynu (ADR-022, etap 1: odczyt).
 *
 * Cały magazyn przychodzi jednym zrzutem (`/api/warsztat/snapshot`) i jest
 * trzymany w localStorage. Dzięki temu:
 *   * aplikacja otwiera się od razu ostatnim stanem, a świeży dociąga w tle
 *     (pierwsze zebranie zrzutu po stronie serwera trwa kilka sekund);
 *   * szukanie w trakcie pisania nie czeka na sieć po każdej literze;
 *   * bez zasięgu w piwnicy nadal widać, co jest i gdzie leży.
 * Szczegóły części (opis, zdjęcia, datasheet) dociąga dopiero karta.
 *
 * Bez frameworka: widok to funkcje zwracające HTML, a kliknięcia obsługuje
 * jeden delegowany handler po atrybucie `data-act`.
 */
(() => {
'use strict';

const APP_VERSION = 25;
const SNAP_KEY = 'wz-snapshot';
const RECENT_KEY = 'wz-recent';
const THEME_KEY = 'wz-theme';
const CATVIEW_KEY = 'wz-catview';
const RESULTS_STEP = 60;
const DESK = window.matchMedia('(min-width: 900px)');

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
  {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
/* Wyszukiwarka nie zna ogonków ani wielkości liter: „łącze” = „lacze”,
   „µF” = „uF”, „Ω” znika, bo nikt go nie wpisuje z klawiatury. */
const norm = (text) => String(text || '').toLowerCase().normalize('NFD')
  .replace(/[̀-ͯ]/g, '').replace(/ł/g, 'l').replace(/[µμ]/g, 'u').replace(/[ωΩ]/g, '');
const plural = (n, one, few, many) => {
  if (n === 1) return one;
  if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return few;
  return many;
};
const parts = (n) => `${n} ${plural(n, 'część', 'części', 'części')}`;

const ICONS = {
  search: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15.5" y1="15.5" x2="21" y2="21"/></svg>',
  browse: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></svg>',
  lists: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="3.5" y="4" width="4" height="4" rx="1"/><rect x="3.5" y="10" width="4" height="4" rx="1"/><rect x="3.5" y="16" width="4" height="4" rx="1"/><line x1="11" y1="6" x2="21" y2="6"/><line x1="11" y1="12" x2="21" y2="12"/><line x1="11" y1="18" x2="21" y2="18"/></svg>',
  add: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="3.5" y="3.5" width="17" height="17" rx="4"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
};

const state = {
  snap: null, byId: new Map(), catById: new Map(), boxById: new Map(), hay: new Map(),
  loading: true, error: '', offline: !navigator.onLine,
  tab: 'search', query: '', shown: RESULTS_STEP,
  flt: {m: null, pk: null}, sort: 'auto',
  browseMode: 'cat', catView: localStorage.getItem(CATVIEW_KEY) || 'list', catId: null, boxId: null, listId: null,
  partId: null, sheet: null, sortBox: null, toastOpts: null, cards: new Map(), photoIdx: 0, techOpen: false, viewer: false,
  recent: readJson(RECENT_KEY, []),
};

function readJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function writeJson(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* pełny magazyn — trudno */ }
}

/* --- dane ------------------------------------------------------------ */

function useSnapshot(snap) {
  state.snap = snap;
  state.byId = new Map(snap.parts.map((p) => [p.id, p]));
  state.catById = new Map(snap.categories.map((c) => [c.id, c]));
  state.boxById = new Map(snap.boxes.map((b) => [b.id, b]));
  state.hay = new Map(snap.parts.map((p) => {
    const cat = state.catById.get(p.cat);
    const boxes = p.locs.map((l) => (state.boxById.get(l.box) || {}).code || '').join(' ');
    const text = norm([p.name, p.s, cat ? cat.name : '', boxes].join(' '));
    return [p.id, [text, text.replace(/\s+/g, ''), norm(p.name)]];
  }));
}

async function loadSnapshot() {
  const cached = readJson(SNAP_KEY, null);
  if (cached && cached.parts) {
    useSnapshot(cached);
    state.loading = false;
    render();
  }
  try {
    const response = await fetch('/api/warsztat/snapshot', {cache: 'no-store'});
    if (!response.ok) throw new Error(`serwer odpowiedział ${response.status}`);
    const snap = await response.json();
    writeJson(SNAP_KEY, snap);
    useSnapshot(snap);
    state.error = '';
    state.offline = false;
  } catch (err) {
    state.offline = !navigator.onLine || !cached;
    if (!cached) state.error = `Nie mogę pobrać stanu magazynu: ${err.message}`;
  }
  state.loading = false;
  render();
}

async function loadCard(id) {
  if (state.cards.has(id)) return;
  try {
    const response = await fetch(`/api/warsztat/parts/${id}`);
    if (!response.ok) throw new Error(String(response.status));
    state.cards.set(id, await response.json());
  } catch {
    state.cards.set(id, {failed: true});
  }
  if (state.partId === id) renderCard();
}

/* --- widoki pomocnicze ----------------------------------------------- */

const boxCode = (id) => (state.boxById.get(id) || {}).code || '?';
const bigPhoto = (url) => (url ? url.replace('w=160', 'w=1024') : url);
const bg = (url) => (url ? ` style="background-image:url('${esc(url)}')"` : '');

function qtyChip(p) {
  const q = p.q;
  if (q.k === 'exact') return `<span class="chip">×${esc(q.n)}</span>`;
  if (q.k === 'approx') return `<span class="chip approx">~${esc(q.n)}</span>`;
  if (q.k === 'some') return '<span class="chip some">jest</span>';
  return '<span class="chip none">nie mam</span>';
}

/* Wiersz części — komponent „PartRow” z projektu. */
function row(p, action) {
  const thumb = p.thumb
    ? `<div class="thumb stripes"${bg(p.thumb)}></div>`
    : '<div class="thumb none">brak<br>zdjęcia</div>';
  const box = p.locs.length
    ? `<span class="chip box">${esc(boxCode(p.locs[0].box))}${p.locs.length > 1 ? ` +${p.locs.length - 1}` : ''}</span>`
    : (p.q.k === 'none' ? '' : '<span class="chip nobox">bez pudła</span>');
  const sel = DESK.matches && state.partId === p.id ? ' sel' : '';
  // W trybie sortowania stuknięcie wkłada część do pudełka zamiast otwierać kartę.
  const act = action ? action.act : 'part';
  return `<button class="row${sel}" data-act="${act}" data-id="${p.id}">${thumb}
    <div class="row-body"><div class="row-name">${esc(p.name)}</div>
      <div class="chips">${qtyChip(p)}${p.low ? '<span class="chip low">kończy się</span>' : ''}${box}${p.chk ? '<span class="chip chk">? do sprawdzenia</span>' : ''}</div>
    </div>${action ? `<span class="row-action">${esc(action.label)}</span>` : '<span class="chev">›</span>'}</button>`;
}

/* --- filtry: montaż, obudowa, kolejność -------------------------------

   Montaż (SMD/THT), obudowę i wartość wyczytuje z nazwy serwer
   (`bom_agent/facets.py`). Filtr pokazuje się dopiero wtedy, gdy jest z czego
   wybierać: lista samych rezystorów THT nie potrzebuje przełącznika SMD/THT.
   „?” zbiera części bez rozpoznanej obudowy — chowanie ich pod
   najbardziej prawdopodobną wartością ukryłoby je, gdy ich szukasz. */

const UNKNOWN = '?';
const mountOf = (p) => p.m || UNKNOWN;
const pkgOf = (p) => p.pk || UNKNOWN;

function byMount(list) {
  return state.flt.m ? list.filter((p) => mountOf(p) === state.flt.m) : list;
}

function applyFilters(list, fallback = 'keep') {
  let out = byMount(list);
  if (state.flt.pk) out = out.filter((p) => pkgOf(p) === state.flt.pk);
  const sort = effectiveSort(list, fallback);
  const copy = [...out];
  if (sort === 'val') copy.sort((a, b) => (a.v ?? Infinity) - (b.v ?? Infinity) || a.name.localeCompare(b.name, 'pl'));
  else if (sort === 'name') copy.sort((a, b) => a.name.localeCompare(b.name, 'pl'));
  else if (sort === 'qty') copy.sort((a, b) => (b.q.n ?? -1) - (a.q.n ?? -1));
  return copy;
}

/* Domyślnie po wartości, gdy większość listy ją ma (rezystory, kondensatory):
   10 Ω, 22 Ω, 100 Ω… czyta się jak taśmę w pudełku. Inaczej kolejność
   zostaje taka, jaką dało szukanie albo kategoria. */
function effectiveSort(list, fallback = 'keep') {
  if (state.sort !== 'auto') return state.sort;
  const withValue = list.filter((p) => p.v != null).length;
  return withValue && withValue >= list.length / 2 ? 'val' : fallback;
}

function counted(list, key) {
  const counts = new Map();
  for (const p of list) counts.set(key(p), (counts.get(key(p)) || 0) + 1);
  return counts;
}

function chipGroup(label, act, counts, current, order) {
  const keys = [...counts.keys()].sort(order);
  const total = [...counts.values()].reduce((a, b) => a + b, 0);
  const chip = (value, text, n) => `<button class="fchip${current === value ? ' on' : ''}" data-act="${act}" data-id="${esc(value ?? '')}">${esc(text)}${n != null ? ` <span>${n}</span>` : ''}</button>`;
  return `<div class="fgroup"><span class="flabel">${label}</span><div class="fchips">
    ${chip(null, 'Wszystkie', total)}${keys.map((k) => chip(k, k === UNKNOWN ? 'nie wiadomo' : k, counts.get(k))).join('')}</div></div>`;
}

// Rozmiary chipów od najmniejszego, dalej alfabetycznie, „?” na końcu.
const CHIP_ORDER = ['01005', '0201', '0402', '0603', '0805', '1206', '1210', '1812', '2010', '2512'];
function pkgOrder(a, b) {
  if (a === UNKNOWN) return 1;
  if (b === UNKNOWN) return -1;
  const ia = CHIP_ORDER.indexOf(a), ib = CHIP_ORDER.indexOf(b);
  if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  return a.localeCompare(b, 'pl', {numeric: true});
}

function facetBar(list, fallback = 'keep') {
  if (list.length < 4) return '';
  const groups = [];
  const mounts = counted(list, mountOf);
  if (mounts.size > 1 || state.flt.m) {
    groups.push(chipGroup('Montaż', 'fm', mounts, state.flt.m, (a, b) => (a === UNKNOWN) - (b === UNKNOWN) || a.localeCompare(b)));
  }
  const pkgs = counted(byMount(list), pkgOf);
  if (pkgs.size > 1 || state.flt.pk) groups.push(chipGroup('Obudowa', 'fpk', pkgs, state.flt.pk, pkgOrder));
  const hasValue = list.some((p) => p.v != null);
  const sorts = [...(hasValue ? [['val', 'Wartość']] : []), ['name', 'Nazwa'], ['qty', 'Ilość']];
  const current = effectiveSort(list, fallback);
  groups.push(`<div class="fgroup"><span class="flabel">Kolejność</span><div class="fchips">${sorts.map(([v, l]) =>
    `<button class="fchip${current === v ? ' on' : ''}" data-act="fsort" data-id="${v}">${l}</button>`).join('')}</div></div>`);
  return `<div class="facets">${groups.join('')}</div>`;
}

function resetFilters() { state.flt = {m: null, pk: null}; state.sort = 'auto'; }

function rows(list, action) {
  if (!list.length) return '';
  const visible = list.slice(0, state.shown);
  const rest = list.length - visible.length;
  return `<div class="stack">${visible.map((p) => row(p, action)).join('')}</div>${rest > 0
    ? `<button class="btn outline more" data-act="more">Pokaż kolejne (zostało ${rest})</button>` : ''}`;
}

function skeletons() {
  return `<div class="stack">${[1, .75, .5, .3].map((o) => `<div class="skeleton" style="opacity:${o}">
    <div class="sq"></div><div style="flex:1;display:flex;flex-direction:column;gap:10px">
    <div class="ln" style="width:70%"></div><div class="ln" style="height:12px;width:40%"></div></div></div>`).join('')}</div>`;
}

/* --- listy robocze ---------------------------------------------------- */

function listData() {
  const all = state.snap ? state.snap.parts : [];
  const stocked = all.filter((p) => p.q.k !== 'none');
  const nobox = stocked.filter((p) => !p.locs.length);
  const check = all.filter((p) => p.chk);
  const nods = stocked.filter((p) => !p.ds && !p.dsn);
  return {stocked, nobox, check, nods};
}

function listTiles(compact) {
  const d = listData();
  const done = d.stocked.length - d.nobox.length;
  const pct = d.stocked.length ? Math.round(done / d.stocked.length * 100) : 100;
  const tiles = [
    {id: 'nobox', name: 'Bez pudła', desc: 'Części, które nie mają jeszcze miejsca.', count: d.nobox.length, color: 'var(--accentInk)',
      progress: `zostało ${d.nobox.length} z ${d.stocked.length}`, pct},
    {id: 'check', name: 'Do sprawdzenia', desc: 'Rozpoznanie ze zdjęcia było niepewne.', count: d.check.length, color: 'var(--text)'},
    {id: 'nods', name: 'Bez datasheetu', desc: 'Brak pliku PDF z dokumentacją.', count: d.nods.length, color: 'var(--text)'},
  ];
  if (compact) {
    return tiles.map((t) => `<button class="tile-row" data-act="list" data-id="${t.id}">
      <span class="num" style="color:${t.color}">${t.count}</span><span class="name">${esc(t.name)}</span><span class="chev">›</span></button>`).join('');
  }
  return tiles.map((t) => `<button class="list-tile" data-act="list" data-id="${t.id}">
    <div class="top"><div style="flex:1"><div class="nm">${esc(t.name)}</div><div class="ds">${esc(t.desc)}</div></div>
    <span class="ct" style="color:${t.color}">${t.count}</span></div>
    ${t.progress ? `<div class="bar"><div style="width:${t.pct}%"></div></div><div class="cat-count">${t.progress}</div>` : ''}
  </button>`).join('');
}

/* --- zakładki ---------------------------------------------------------- */

function renderTabs() {
  const nobox = state.snap ? listData().nobox.length : 0;
  const tabs = [['search', 'Szukaj'], ['browse', 'Przeglądaj'], ['lists', 'Listy'], ['add', 'Dodaj']];
  const html = tabs.map(([id, label]) => {
    const badge = id === 'lists' && nobox > 0 ? `<span class="badge">${nobox}</span>` : '';
    // „Dodaj” to strona intake'u pod /dodaj — ma własną logikę aparatu
    // i poczekalni offline, więc jest osobną stroną z tym samym paskiem.
    if (id === 'add') {
      // Odznaka = propozycje do zatwierdzenia: jedyne, co tu czeka na mnie.
      const n = add.proposals.length;
      return `<button class="tab${state.tab === id ? ' on' : ''}" data-act="tab" data-id="add">
        <span class="pill">${ICONS.add}</span><span>${label}</span>${n ? `<span class="badge">${n}</span>` : ''}</button>`;
    }
    return `<button class="tab${state.tab === id ? ' on' : ''}" data-act="tab" data-id="${id}">
      <span class="pill">${ICONS[id]}</span><span>${label}</span>${badge}</button>`;
  }).join('');
  $('tabbar').innerHTML = html;
  $('rail').innerHTML = `<div class="logo" title="Colorbox Studio · Warsztat"><svg viewBox="2 8 104 104" aria-hidden="true"><polygon class="ink" points="28,58 58,75 58,110 28,93"/><polygon class="ink" points="62,75 92,58 92,93 62,110"/><polygon class="ink" points="24,52 56,35 44,10 12,27"/><polygon points="60,50 68,58 60,66 52,58" fill="#D4553A"/><polygon points="70,22 78,30 70,38 62,30" fill="#E2A33B"/><polygon points="87,14 93,20 87,26 81,20" fill="#2E8C86"/></svg></div>${html}<div style="flex:1"></div>
    <button class="tab" data-act="open-settings"><span class="pill gear">⚙&#xFE0E;</span><span>Ustawienia</span></button>`;
}

/* --- Szukaj ------------------------------------------------------------ */

function search(query) {
  // „#591” albo sam numer to identyfikator części — trafienie dokładne
  // idzie pierwsze, przed częściami, które mają 591 w nazwie.
  const idMatch = query.trim().match(/^#?(\d+)$/);
  const exact = idMatch ? state.byId.get(Number(idMatch[1])) : null;
  if (query.trim().startsWith('#')) return exact ? [exact] : [];
  const found = searchText(query);
  return exact ? [exact, ...found.filter((p) => p !== exact)] : found;
}

function searchText(query) {
  const tokens = norm(query).split(/\s+/).filter(Boolean);
  if (!tokens.length) return [];
  const hits = [];
  for (const p of state.snap.parts) {
    const [text, squashed, name] = state.hay.get(p.id);
    if (!tokens.every((t) => text.includes(t) || squashed.includes(t))) continue;
    // Trafienie w nazwie przed trafieniem w opisie, krótsza nazwa przed
    // dłuższą: „555” ma pokazać NE555, a nie moduł z 555 w opisie.
    const inName = tokens.every((t) => name.includes(t)) ? 0 : 1;
    hits.push([inName, name.length, p]);
  }
  hits.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return hits.map((h) => h[2]);
}

function gearButton() {
  // Na telefonie ustawienia są przy polu szukania, na komputerze na szynie.
  return '<button class="gear-btn" data-act="open-settings"><span>⚙&#xFE0E;</span>Ustawienia</button>';
}

function searchView() {
  return `<div class="head"><div class="search">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" stroke-width="2.4"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15.5" y1="15.5" x2="21" y2="21"/></svg>
      <input id="q" type="search" autocomplete="off" enterkeyhint="search" value="${esc(state.query)}" placeholder="Czego szukasz? np. 555, 10k, ESP32">
      <button class="clear" data-act="clear" ${state.query ? '' : 'hidden'} aria-label="Wyczyść">×</button>
    </div>${gearButton()}</div>
    <div class="scroll" id="results">${resultsHtml()}</div>`;
}

function resultsHtml() {
  if (state.error) return `<div class="empty"><div class="empty-title">Brak danych</div><div class="empty-text">${esc(state.error)}</div></div>`;
  if (state.loading) return `<div class="label">Wczytuję magazyn…</div>${skeletons()}`;
  const q = state.query.trim();
  if (!q) {
    const recent = state.recent.map((id) => state.byId.get(id)).filter(Boolean).slice(0, 4);
    return `${recent.length ? `<div class="label">Ostatnio oglądane</div><div class="stack">${recent.map(row).join('')}</div>` : ''}
      <div class="label${recent.length ? ' gap' : ''}">Listy robocze</div><div class="stack">${listTiles(true)}</div>`;
  }
  const found = search(q);
  if (!found.length) {
    return `<div class="empty"><div class="ph-dashed"></div>
      <div class="empty-title">Nie mam nic na „${esc(q)}”</div>
      <div class="empty-text">Sprawdź pisownię albo wpisz krócej. Jeśli masz to w ręku, dodaj ze zdjęcia.</div>
      <button class="btn accent" data-act="tab" data-id="add">Dodaj ze zdjęcia</button></div>`;
  }
  const shown = applyFilters(found);
  const count = shown.length === found.length
    ? `${found.length} ${plural(found.length, 'wynik', 'wyniki', 'wyników')}`
    : `${shown.length} z ${found.length} ${plural(found.length, 'wyniku', 'wyników', 'wyników')}`;
  return `${facetBar(found)}<div class="label">${count}</div>${rows(shown)}`;
}

/* --- Przeglądaj -------------------------------------------------------- */

function catParts(id) {
  const ids = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const c of state.snap.categories) if (c.parent && ids.has(c.parent) && !ids.has(c.id)) { ids.add(c.id); grew = true; }
  }
  return state.snap.parts.filter((p) => ids.has(p.cat));
}

function catTile(c) {
  const subs = state.snap.categories.filter((x) => x.parent === c.id).length;
  const thumbs = catParts(c.id).filter((p) => p.thumb).slice(0, 3).map((p) => p.thumb);
  return `<button class="cat-tile" data-act="cat" data-id="${c.id}">
    <div class="mosaic">${[0, 1, 2].map((i) => `<div${bg(thumbs[i])}></div>`).join('')}</div>
    <div class="cat-name">${esc(c.name)}</div>
    <div class="cat-count">${parts(c.total)}${subs ? ` · ${subs} podkat.` : ''}</div></button>`;
}

/* Zwykłe drzewo kategorii: alfabetycznie, z liczbą części, podkategorie
   wcięte pod rodzicem. Kafelki są dobre do oglądania, ale żeby znaleźć
   „Transoptory” wśród 23 pozycji, szybciej czyta się listę. */
function catTree() {
  const cats = state.snap.categories;
  const byName = (a, b) => a.name.localeCompare(b.name, 'pl');
  const out = [];
  const walk = (parent, level) => cats.filter((c) => (c.parent || null) === parent && c.total)
    .sort(byName).forEach((c) => {
      const kids = cats.some((x) => x.parent === c.id && x.total);
      out.push(`<button class="tree-row" data-act="cat" data-id="${c.id}" style="padding-left:${14 + level * 22}px">
        <span class="tree-name${level ? ' sub' : ''}">${esc(c.name)}${kids ? ' <span class="tree-kids">▾</span>' : ''}</span>
        <span class="tree-n">${c.total}</span><span class="chev">›</span></button>`);
      walk(c.id, level + 1);
    });
  walk(null, 0);
  return `<div class="tree">${out.join('')}</div>`;
}

function boxPhoto(box, cls) {
  return box.photo ? `<div class="box-ph stripes ${cls || ''}"${bg(box.photo)}></div>` : `<div class="box-ph ph-dashed ${cls || ''}"></div>`;
}

function boxRow(box, level) {
  return `<div style="padding-left:${level * 18}px"><button class="box-row" data-act="box" data-id="${box.id}">
    ${boxPhoto(box, level === 0 ? 'l0' : '')}
    <div style="flex:1;min-width:0"><div class="box-code">${esc(box.code)}</div><div class="box-name">${esc(box.name)}</div></div>
    <span class="box-n">${box.n ? parts(box.n) : 'pusto'}</span><span class="chev">›</span></button></div>`;
}

function browseView() {
  const s = state;
  const cat = s.catId != null ? s.catById.get(s.catId) : null;
  const box = s.boxId != null ? s.boxById.get(s.boxId) : null;
  let back = '';
  if (s.browseMode === 'cat' && cat) {
    const parent = cat.parent ? s.catById.get(cat.parent) : null;
    back = `<button class="btn small" data-act="cat" data-id="${parent ? parent.id : ''}">‹ ${esc(parent ? parent.name : 'Kategorie')}</button>`;
  }
  if (s.browseMode === 'box' && box) {
    const parent = box.parent ? s.boxById.get(box.parent) : null;
    back = `<button class="btn small" data-act="box" data-id="${parent ? parent.id : ''}">‹ ${esc(parent ? parent.name : 'Pudełka')}</button>`;
  }
  const seg = [['cat', 'Kategorie'], ['box', 'Pudełka']].map(([v, l]) =>
    `<button class="${s.browseMode === v ? 'on' : ''}" data-act="mode" data-id="${v}">${l}</button>`).join('');

  let body = '';
  if (s.loading) body = skeletons();
  else if (s.browseMode === 'cat' && !cat) {
    const switcher = `<div class="view-switch">${[['list', 'Lista'], ['tiles', 'Kafelki']].map(([v, l]) =>
      `<button class="${s.catView === v ? 'on' : ''}" data-act="catview" data-id="${v}">${l}</button>`).join('')}</div>`;
    body = switcher + (s.catView === 'tiles'
      ? `<div class="cat-grid">${s.snap.categories.filter((c) => !c.parent && c.total).map(catTile).join('')}</div>`
      : catTree());
  } else if (s.browseMode === 'cat') {
    const subs = s.snap.categories.filter((x) => x.parent === cat.id);
    body = `<div class="title-line"><div class="t">${esc(cat.name)}</div><div class="n">${parts(cat.total)}</div></div>
      ${subs.length ? `<div class="sub-grid">${subs.map((c) => `<button class="sub-tile" data-act="cat" data-id="${c.id}">
        <div class="box-ph stripes"></div><div style="min-width:0"><div class="cat-name" style="font-size:14px">${esc(c.name)}</div>
        <div class="cat-count" style="font-size:12px;margin-top:3px">${parts(c.total)}</div></div></button>`).join('')}</div>` : ''}
      ${(() => {
        const inCat = s.snap.parts.filter((p) => p.cat === cat.id);
        const shown = applyFilters(inCat, 'name');
        return `${facetBar(inCat, 'name')}${shown.length !== inCat.length ? `<div class="label">${shown.length} z ${inCat.length}</div>` : ''}${rows(shown)}`;
      })()}`;
  } else if (!box && !s.snap.boxes.length) {
    body = `<div class="empty"><div class="ph-dashed"></div><div class="empty-title">Nie masz jeszcze pudełek</div>
      <div class="empty-text">Zacznij od kartonu. W nim kuwety, w kuwetach przegrody — najwyżej trzy poziomy.</div>
      <button class="btn accent" data-act="new-top-box">Utwórz pierwsze pudełko</button></div>`;
  } else if (!box) {
    const out = [];
    const walk = (parent, level) => s.snap.boxes.filter((b) => b.parent === parent)
      .forEach((b) => { out.push(boxRow(b, level)); walk(b.id, level + 1); });
    walk(null, 0);
    body = `<div class="stack tight">${out.join('')}</div>
      <button class="tool dashed" style="margin-top:12px" data-act="new-top-box">+ Nowe pudełko</button>
      <div class="hint2" style="margin-top:8px">Pudełko może stać samo — karton, organizer, zwykłe pudełko; nazwę wpisz własną. Jeśli ma coś w środku, otwórz je i kliknij „Dodaj kuwetę”.</div>`;
  } else {
    const children = s.snap.boxes.filter((b) => b.parent === box.id);
    const inside = s.snap.parts.filter((p) => p.locs.some((l) => l.box === box.id));
    body = `${box.photo
      ? `<div class="box-photo stripes"${bg(bigPhoto(box.photo))}></div>`
      : '<div class="box-photo none"><b style="color:var(--text);font-size:17px">To pudełko nie ma zdjęcia</b><span>Zdjęcie otwartej kuwety pomaga znaleźć część wzrokiem.</span></div>'}
      <div style="margin:14px 4px 0"><div class="box-big">${esc(box.code)}</div><div class="box-path">${esc(box.path)}</div></div>
      ${box.code.split('/').length < 3 ? `<button class="tool outline" style="margin-top:12px" data-act="add-child-box" data-id="${box.id}">${box.code.split('/').length === 1 ? 'Dodaj kuwetę' : 'Dodaj przegrodę'}</button>` : ''}
      ${children.length ? `<div class="label gap">W środku</div><div class="stack tight">${children.map((b) => boxRow(b, 0)).join('')}</div>` : ''}
      <div class="label gap">${inside.length ? `Zawartość · ${inside.length}` : 'Zawartość'}</div>
      ${inside.length ? rows(inside) : (children.length ? '' : `<div class="note-box">Pusto. Wrzuć tu części z listy „Bez pudła”.
        <button class="btn accent" style="margin-top:12px" data-act="sort-into" data-id="${box.id}">Sortuj do ${esc(box.code)}</button></div>`)}`;
  }
  return `<div class="head"><div class="seg">${seg}</div>${back ? `<div>${back}</div>` : ''}</div>
    <div class="scroll">${body}</div>`;
}

/* --- Listy -------------------------------------------------------------- */

function listsView() {
  if (state.loading) return `<div class="scroll">${skeletons()}</div>`;
  if (!state.listId) {
    return `<div class="scroll"><div class="title-bar"><div class="page-title">Listy robocze</div>${gearButton()}</div>
      <div class="page-sub">Odhaczaj po kolei. Każda zrobiona rzecz znika.</div>
      <div class="stack" style="gap:10px">${listTiles(false)}</div></div>`;
  }
  const d = listData();
  const map = {nobox: ['Bez pudła', d.nobox], check: ['Do sprawdzenia', d.check], nods: ['Bez datasheetu', d.nods]};
  const [title, list] = map[state.listId];
  let extra = '';
  if (state.listId === 'nobox') {
    const done = d.stocked.length - d.nobox.length;
    const pct = d.stocked.length ? Math.round(done / d.stocked.length * 100) : 100;
    extra = `<div class="progress"><div class="top"><span>Zostało <span class="mono">${d.nobox.length}</span> z <span class="mono">${d.stocked.length}</span></span>
      <span class="cat-count" style="color:var(--ok)">${done} zrobione</span></div><div class="bar"><div style="width:${pct}%"></div></div></div>`;
    const box = state.sortBox != null ? state.boxById.get(state.sortBox) : null;
    extra = box
      ? `<div class="sort-bar"><div style="flex:1;min-width:0"><small>Wkładasz do</small><b class="mono">${esc(box.code)}</b><span>${esc(box.path)}</span></div>
          <button data-act="start-sort">Zmień</button><button class="end" data-act="stop-sort">Koniec</button></div>
        <div class="hint2">Stuknij część — trafi do ${esc(box.code)} i zniknie z listy.</div>${extra}`
      : `${extra}<button class="btn accent wide" data-act="start-sort">Zacznij sortowanie</button>`;
  }
  const sorting = state.listId === 'nobox' && state.sortBox != null;
  return `<div class="head"><div style="display:flex;align-items:center;gap:10px">
      <button class="btn small" data-act="list" data-id="">‹ Listy</button><div style="font-size:20px;font-weight:700">${esc(title)}</div></div>${extra}</div>
    <div class="scroll">${list.length ? rows(list, sorting ? {act: 'sort-put', label: 'Włóż'} : null) : `<div class="empty"><div class="check-circle">✓</div>
      <div class="empty-title">Lista pusta</div><div class="empty-text">Nic tu nie zostało do zrobienia.</div></div>`}</div>`;
}

/* --- karta części (projekt „Warsztat v2”, ADR-022 etap 2) -------------

   Każdy obszar karty to osobny blok z własnym nagłówkiem i własnym
   przyciskiem „Edytuj”, który otwiera arkusz tylko z polami tego bloku.
   Jeden wielki formularz „edytuj wszystko” byłby na telefonie ścianą pól.

   Zapis idzie na serwer od razu i wraca świeża karta razem z wierszem
   listy — podmieniamy jedną część, bez pobierania całego magazynu. */

const PRECISE_KEY = 'bom.precise_camera'; // wspólny z ekranem „Dodaj”
const PKGS = {
  SMD: ['0402', '0603', '0805', '1206', 'SOT-23', 'SOT-223', 'SOIC-8', 'SOIC-16', 'TSSOP-20', 'QFN-32'],
  THT: ['DIP-8', 'DIP-14', 'DIP-16', 'DIP-28', 'TO-92', 'TO-220', 'TO-247', 'THT 0207', 'HC-49'],
};
const qText = (q) => (q.k === 'exact' ? `×${q.n}` : q.k === 'approx' ? `~${q.n}` : q.k === 'some' ? 'jest' : 'nie mam');

async function api(method, url, body) {
  const options = {method, headers: {}};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  let data = null;
  try { data = await response.json(); } catch { /* pusta odpowiedź */ }
  if (!response.ok) throw new Error((data && (data.detail || data.error)) || `serwer odpowiedział ${response.status}`);
  return data;
}

/* Świeża karta z serwera → podmiana wiersza w zrzucie i przerysowanie. */
function applyCard(card) {
  state.cards.set(card.id, card);
  const index = state.snap.parts.findIndex((p) => p.id === card.id);
  if (index >= 0) state.snap.parts[index] = card.row;
  else state.snap.parts.push(card.row);
  useSnapshot(state.snap);
  writeJson(SNAP_KEY, state.snap);
  renderTabs();
  state.keepScroll = true;
  renderView();
  renderCard();
}

/* Zapis z komunikatem i — gdy się da — „Cofnij”. `undo` to funkcja, która
   wykonuje odwrotny zapis. */
async function save(promise, message, undo) {
  try {
    const card = await promise;
    if (card && card.row) applyCard(card);
    toast(message, undo ? {undo} : {});
    return card;
  } catch (err) {
    toast(`Nie zapisano: ${err.message}`, {bad: true});
    return null;
  }
}

const editPart = (id, changes) => api('POST', `/api/warsztat/parts/${id}`, changes);

/* --- komunikat ------------------------------------------------------- */

let toastTimer = null;
function toast(message, opts = {}) {
  const el = $('toast');
  clearTimeout(toastTimer);
  el.className = `toast${opts.bad ? ' bad' : ''}`;
  el.innerHTML = `<span class="toast-msg">${esc(message)}</span>
    ${opts.action ? `<button class="toast-act" data-act="toast-action">${esc(opts.action.label)}</button>` : ''}
    ${opts.undo ? '<button class="toast-undo" data-act="toast-undo">Cofnij</button>' : ''}`;
  el.hidden = false;
  state.toastOpts = opts;
  toastTimer = setTimeout(() => { el.hidden = true; state.toastOpts = null; }, opts.undo || opts.action ? 5000 : 2400);
}

/* --- karta ----------------------------------------------------------- */

const block = (label, body, editAct) => `<section class="blk">
  <div class="blk-head"><span class="blk-label">${label}</span>${editAct ? `<button class="blk-edit" data-act="${editAct}">✎ Edytuj</button>` : ''}</div>
  ${body}</section>`;

function renderCard() {
  const pane = $('card-pane');
  const p = state.partId != null ? state.byId.get(state.partId) : null;
  $('desk-empty').hidden = !!p || state.tab === 'add';
  if (!p) { pane.hidden = true; pane.innerHTML = ''; return; }
  pane.hidden = false;
  const card = state.cards.get(p.id) || {};
  const photos = card.photos && card.photos.length ? card.photos : (p.thumb ? [{url: bigPhoto(p.thumb), main: true}] : []);
  const idx = Math.min(state.photoIdx, Math.max(0, photos.length - 1));
  const current = photos[idx];

  // Zdjęcie
  const photoBlock = photos.length
    ? `<section class="blk blk-photo">
        <div class="photo" id="photo"><img src="${esc(current.url)}" alt="">
          ${current.main && photos.length > 1 ? '<span class="main-badge">Zdjęcie główne</span>' : ''}
          ${photos.length > 1 ? `<div class="dots">${photos.map((_, i) => `<span class="${i === idx ? 'on' : ''}"></span>`).join('')}<b>${idx + 1} / ${photos.length}</b></div>` : ''}
        </div>
        <div class="photo-tools">
          ${photos.length > 1 ? `<div class="strip">${photos.map((ph, i) => `<button class="strip-th${i === idx ? ' on' : ''}" data-act="photo-idx" data-id="${i}" style="background-image:url('${esc(ph.url)}')"></button>`).join('')}</div>` : ''}
          <div class="grid3">
            <button class="tool" data-act="photo-add">Dodaj zdjęcie</button>
            <button class="tool" data-act="photo-main" ${current.main || current.id == null ? 'disabled' : ''}>${current.main ? 'Jest główne' : 'Ustaw jako główne'}</button>
            <button class="tool bad" data-act="photo-remove" ${current.id == null ? 'disabled' : ''}>Usuń zdjęcie</button>
          </div>
        </div></section>`
    : `<section class="blk blk-photo"><div class="photo none">
        <b>Ta część nie ma zdjęcia</b><span>Zdjęcie pomaga znaleźć ją wzrokiem w pudełku.</span>
        <button class="btn accent" data-act="photo-add">Zrób zdjęcie</button></div></section>`;

  // Nazwa
  const nameBlock = block('Nazwa', `<div class="card-name">${esc(p.name)}</div>
    <div class="id-line"><button class="id-chip mono" data-act="copy-link" title="Kopiuj link do tej części">#${p.id}</button>
      <button class="tool small-tool" data-act="copy-link">Kopiuj link</button>
      ${navigator.share ? '<button class="tool small-tool" data-act="share-link">Udostępnij</button>' : ''}
      ${card.partdb_url ? `<a class="link-btn" href="${esc(card.partdb_url)}" target="_blank" rel="noopener">Part-DB ↗</a>` : ''}</div>
    ${p.chk ? `<div class="check-bar"><div><b>? do sprawdzenia</b><span>Rozpoznanie ze zdjęcia jest niepewne.</span></div>
      <button data-act="checked">Sprawdzone</button></div>` : ''}`, 'ed-name');

  // Ilość
  const q = p.q;
  let big;
  if (q.k === 'exact') big = `<div class="qty-big"><b>${esc(q.n)}</b><span>szt.</span></div>`;
  else if (q.k === 'approx') big = `<div class="qty-big"><b class="approx">~${esc(q.n)}</b><span>szt.</span></div>`;
  else if (q.k === 'some') big = '<div class="qty-some">Jest</div>';
  else big = '<div class="qty-none">Nie mam</div>';
  const qLabel = {exact: 'policzone dokładnie', approx: 'mniej więcej — nie liczone co do sztuki', some: 'mam, ilość nieoszacowana', none: 'wpis katalogowy — znam tę część, ale jej nie mam'}[q.k];
  const canTake = q.k === 'exact' && q.n > 0;
  const qtyBlock = block('Ilość', `${big}
    <div class="qty-meta"><span class="qty-label">${qLabel}</span>${p.low ? '<span class="chip low">kończy się — dokup</span>' : ''}</div>
    ${canTake ? '<div class="grid2"><button class="tool big" data-act="take">−1 wziąłem</button><button class="tool outline" data-act="ed-qty">Zmień ilość</button></div>'
    : '<button class="tool outline" data-act="ed-qty">Zmień ilość</button>'}`);

  // Gdzie leży
  const locs = p.locs.map((l) => {
    const box = state.boxById.get(l.box) || {code: '?', path: ''};
    return `<div class="loc-card">
      <button class="loc" data-act="box" data-id="${l.box}">${box.photo ? `<div class="box-ph stripes"${bg(box.photo)}></div>` : '<div class="box-ph ph-dashed nophoto">brak<br>zdjęcia</div>'}
        <div style="flex:1;min-width:0"><div class="loc-code">${esc(box.code)}</div><div class="loc-path">${esc(box.path)}</div></div>
        ${l.n != null ? `<span class="loc-n">${esc(l.n)} szt.</span>` : ''}</button>
      <div class="grid2"><button class="tool" data-act="box-move" data-id="${l.box}">Przenieś</button>
        <button class="tool outline" data-act="box-remove" data-id="${l.box}">Usuń z tego pudełka</button></div></div>`;
  }).join('');
  const whereBody = p.locs.length
    ? `${locs}<button class="tool dashed" data-act="assign">+ Przypisz kolejne pudełko</button>`
    : (q.k === 'none' ? '<div class="muted">Wpis katalogowy — nie ma czego włożyć do pudełka.</div>'
      : `<div class="nobox-card"><b>Bez pudła</b><span>Nie wiadomo, gdzie leży. Włóż do pudełka i zapisz to tutaj.</span>
        <button class="btn accent" data-act="assign">Przypisz pudełko</button></div>`);
  const whereBlock = block(`Gdzie leży${p.locs.length ? ` <span class="blk-count">${p.locs.length} ${plural(p.locs.length, 'pudełko', 'pudełka', 'pudełek')}</span>` : ''}`, whereBody);

  const what = card.failed ? 'Nie udało się pobrać opisu — sprawdź połączenie.' : card.what;
  const whatBlock = block('Co to jest', what ? `<div class="txt">${esc(what)}</div>` : `<div class="muted">${card.what === undefined ? 'Wczytuję…' : 'Brak opisu.'}</div>`, 'ed-what');
  const notesBlock = block('Uwagi', card.notes ? `<div class="txt">${esc(card.notes)}</div>` : '<div class="muted">Brak uwag.</div>', 'ed-notes');

  const cat = state.catById.get(p.cat);
  const catBlock = block('Kategoria', `<button class="pick-row" data-act="ed-cat"><span>${esc(card.cat_path || (cat ? cat.name : 'bez kategorii'))}</span><b>Zmień</b></button>
    ${p.cat != null ? `<button class="link-btn" data-act="cat" data-id="${p.cat}">Pokaż inne z tej kategorii ›</button>` : ''}`);

  // Dokumenty
  const dsKind = card.datasheet || (p.ds ? 'file' : (p.dsn ? 'none' : 'search'));
  let ds;
  if (dsKind === 'file' && card.datasheets && card.datasheets.length) ds = `<a class="big-btn" href="${esc(card.datasheets[0].url)}" target="_blank" rel="noopener">Datasheet<small class="ok">otwórz PDF</small></a>`;
  else if (dsKind === 'none') ds = '<span class="big-btn off">Datasheet<small>nie dotyczy</small></span>';
  else ds = `<a class="big-btn dashed" href="https://www.google.com/search?q=${encodeURIComponent(`${p.name} datasheet pdf`)}" target="_blank" rel="noopener">Datasheet<small class="acc">poszukaj w sieci</small></a>`;
  const shop = card.shops && card.shops.length
    ? `<a class="big-btn" href="${esc(card.shops[0].url)}" target="_blank" rel="noopener">Sklep<small>${esc(card.shops[0].name)} ↗</small></a>`
    : '<span class="big-btn off">Sklep<small>brak linku</small></span>';
  const docsBlock = block('Dokumenty', `<div class="grid2">${ds}${shop}</div>
    <button class="tool dashed" data-act="ed-addlink">+ Dodaj link</button>`, 'ed-links');

  // Dane techniczne
  const tech = card.tech || [];
  const techBlock = `<section class="blk"><button class="tech-toggle" data-act="tech"><span class="blk-label">Dane techniczne</span><b>${state.techOpen ? 'Zwiń' : 'Pokaż'}</b></button>
    ${state.techOpen ? `${tech.length ? tech.map((t) => `<div class="tech-row"><span>${esc(t.k)}</span><span>${esc(t.v)}</span></div>`).join('')
      : '<div class="muted">Brak danych. Uzupełnij producenta, obudowę i parametry.</div>'}
      <button class="tool outline" data-act="ed-tech">✎ Edytuj dane techniczne</button>` : ''}</section>`;

  pane.innerHTML = `${DESK.matches ? '' : `<div class="card-bar"><button class="btn small" data-act="close">‹ Wróć</button><span class="mono">${esc(p.name)}</span></div>`}
    <div class="card-scroll"><div class="card-inner">
      ${photoBlock}${nameBlock}${qtyBlock}${whereBlock}${whatBlock}${notesBlock}${catBlock}${docsBlock}${techBlock}
      <div class="danger"><button class="btn danger-btn" data-act="ed-delete">Usuń część z magazynu</button>
        <span>Nieodwracalne — zapytam o potwierdzenie.</span></div>
    </div></div>`;

  const el = $('photo');
  if (el && photos.length) {
    let x0 = null;
    el.onpointerdown = (e) => { x0 = e.clientX; };
    el.onpointerup = (e) => {
      if (x0 == null) return;
      const dx = e.clientX - x0; x0 = null;
      if (Math.abs(dx) > 40 && photos.length > 1) step(dx < 0 ? 1 : -1, photos.length);
      else if (Math.abs(dx) < 8) openViewer(photos.map((x) => x.url));
    };
  }
  if (state.viewer) openViewer(photos.map((x) => x.url));
}

function step(delta, count) {
  state.photoIdx = (Math.min(state.photoIdx, count - 1) + delta + count) % count;
  renderCard();
}

function openViewer(photos) {
  state.viewer = true;
  const idx = Math.min(state.photoIdx, photos.length - 1);
  const v = $('viewer');
  v.hidden = false;
  v.innerHTML = `<div class="viewer-top"><span>${idx + 1} / ${photos.length}</span>
    <button class="round" data-act="viewer-close" aria-label="Zamknij">×</button></div>
    <div class="viewer-img" id="viewer-img"><img src="${esc(photos[idx].replace('w=1024', 'w=1568'))}" alt=""></div>
    ${photos.length > 1 ? '<div class="viewer-nav"><button data-act="prev">‹</button><button data-act="next">›</button></div>' : ''}`;
  let x0 = null;
  const img = $('viewer-img');
  img.onpointerdown = (e) => { x0 = e.clientX; };
  img.onpointerup = (e) => {
    if (x0 == null) return;
    const dx = e.clientX - x0; x0 = null;
    if (Math.abs(dx) > 40 && photos.length > 1) { state.photoIdx = (idx + (dx < 0 ? 1 : -1) + photos.length) % photos.length; openViewer(photos); }
  };
}

function closeViewer() { state.viewer = false; $('viewer').hidden = true; $('viewer').innerHTML = ''; }

function openPart(id) {
  state.partId = id;
  state.photoIdx = 0;
  state.techOpen = false;
  state.recent = [id, ...state.recent.filter((x) => x !== id)].slice(0, 8);
  writeJson(RECENT_KEY, state.recent);
  // Adres w pasku = adres tej części. Skopiowany albo udostępniony otwiera
  // dokładnie tę kartę (#c/591), a numer to identyfikator części w Part-DB,
  // po którym agent ją znajdzie.
  history.replaceState(null, '', partPath(id));
  renderCard();
  if (DESK.matches) renderView();
  loadCard(id);
}

const partPath = (id) => `/#c/${id}`;
const partLink = (id) => `${location.origin}${partPath(id)}`;

/* --- arkusze ------------------------------------------------------- */

function openSheet(sheet) { state.sheet = sheet; renderSheet(); }
function closeSheet() { state.sheet = null; renderSheet(); }

function sheetFrame(title, sub, body) {
  return `<div class="scrim" data-act="sheet-close"></div>
    <div class="sheet" role="dialog" aria-label="${esc(title)}">
      <div class="handle"></div>
      <div class="sheet-head"><div style="flex:1;min-width:0"><div class="sheet-title">${esc(title)}</div>${sub ? `<div class="sheet-sub">${esc(sub)}</div>` : ''}</div>
        <button class="round-x" data-act="sheet-close" aria-label="Zamknij">×</button></div>
      <div class="sheet-body">${body}</div></div>`;
}

const segBtns = (opts, current, act) => `<div class="seg2">${opts.map(([v, l]) =>
  `<button class="${current === v ? 'on' : ''}" data-act="${act}" data-id="${esc(v)}">${esc(l)}</button>`).join('')}</div>`;

function boxButton(b, act) {
  return `<button class="box-pick" data-act="${act}" data-id="${b.id}">${b.photo ? `<span class="box-ph stripes"${bg(b.photo)}></span>` : '<span class="box-ph ph-dashed"></span>'}
    <span style="min-width:0"><b class="mono">${esc(b.code)}</b><small>${esc(b.name)}</small></span></button>`;
}

function renderSheet() {
  const host = $('sheet');
  const sh = state.sheet;
  if (!sh) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;
  const p = state.byId.get(sh.pid ?? state.partId);
  const card = p ? state.cards.get(p.id) || {} : {};
  const forPart = p ? `dla: ${p.name}` : '';
  let html = '';

  if (sh.kind === 'settings') {
    const theme = localStorage.getItem(THEME_KEY) || 'dark';
    const precise = localStorage.getItem(PRECISE_KEY) === '1';
    const cam = (value, label, desc) => `<button class="radio-row${precise === value ? ' on' : ''}" data-act="set-cam" data-id="${value ? '1' : '0'}">
      <span class="radio"><span></span></span><span><b>${label}</b><small>${desc}</small></span></button>`;
    html = sheetFrame('Ustawienia', '', `<div class="flabel2">Motyw</div>
      ${segBtns([['dark', 'Ciemny'], ['light', 'Jasny'], ['system', 'Jak w telefonie']], theme, 'set-theme')}
      <div class="flabel2">Aparat</div>
      ${cam(false, 'Zwykły', 'Aparat telefonu, pełna obróbka zdjęcia.')}
      ${cam(true, 'Precyzyjny', 'Aparat w aplikacji, zoom zapamiętany — do drobnego nadruku.')}
      <div class="sheet-foot mono"><span>Warsztat v${APP_VERSION}</span><span><i class="dot" style="background:${state.offline ? 'var(--bad)' : 'var(--ok)'}"></i>${state.offline ? 'offline' : 'połączono'}</span></div>`);
  } else if (sh.kind === 'text') {
    const area = sh.field !== 'name';
    const value = sh.draft ?? '';
    const labels = {name: ['Nazwa', 'Nazwa, po której szukasz — np. „Rezystor 10 kΩ 0,25 W”.'],
      what: ['Co to jest', '1–3 zdania: do czego służy, jak wygląda.'], notes: ['Uwagi', 'Np. „typ nieczytelny na zdjęciu”, „brakuje jednej nóżki”.']}[sh.field];
    html = sheetFrame(labels[0], forPart, `${area
      ? `<textarea id="ed-value" rows="5" class="in big">${esc(value)}</textarea>`
      : `<input id="ed-value" class="in big" value="${esc(value)}">`}
      <div class="hint2">${labels[1]}</div>
      <button class="btn accent wide" data-act="save-text">Zapisz</button>`);
  } else if (sh.kind === 'qty') {
    const d = sh.draft;
    html = sheetFrame('Ilość', forPart, `${segBtns([['exact', 'Dokładnie'], ['approx', 'Mniej więcej'], ['some', 'Jest']], d.k, 'qty-kind')}
      ${d.k !== 'some' ? `<div class="stepper"><button data-act="qty-dec">−</button>
        <div><b class="mono">${d.k === 'approx' ? '~' : ''}${d.n ?? 0}</b><small>${d.k === 'approx' ? 'krok co 10' : 'sztuk'}</small></div>
        <button data-act="qty-inc">+</button></div>` : '<div class="note-box">Mam, ale nie liczę. Wystarczy wiedzieć, że jest.</div>'}
      <button class="switch-row${d.low ? ' on' : ''}" data-act="qty-low">Kończy się<span class="switch"><span></span></span></button>
      <button class="btn accent wide" data-act="save-qty">Zapisz</button>`);
  } else if (sh.kind === 'cat') {
    const qn = norm(sh.query || '');
    const cats = [...state.snap.categories].sort((a, b) => a.name.localeCompare(b.name, 'pl'))
      .filter((c) => !qn || norm(c.name).includes(qn));
    html = sheetFrame('Kategoria', forPart, `<div class="hint2">Stuknij kategorię — zapiszę od razu.</div>
      <div class="search-in"><input id="cat-q" placeholder="Zawęź listę" value="${esc(sh.query || '')}"></div>
      <div class="stack tight">${cats.map((c) => `<button class="cat-opt${c.id === (p && p.cat) ? ' on' : ''}" data-act="pick-cat" data-id="${c.id}">
        <span>${esc(c.name)}</span><small class="mono">${c.total}</small><b>${c.id === (p && p.cat) ? '✓' : ''}</b></button>`).join('')}</div>`);
  } else if (sh.kind === 'links') {
    html = sheetFrame('Dokumenty', forPart, `<div class="flabel2">Datasheet (PDF)</div>
      <input id="lk-ds" class="in mono" placeholder="Wklej adres PDF">
      <button class="switch-row${sh.na ? ' on' : ''}" data-act="lk-na">Datasheet nie dotyczy<span class="switch"><span></span></span></button>
      <div class="flabel2">Sklep</div>
      <input id="lk-shop" class="in mono" placeholder="Wklej adres sklepu">
      <div class="hint2">Istniejące linki zostają; nowy dopisuje się obok.</div>
      <button class="btn accent wide" data-act="save-links">Zapisz</button>`);
  } else if (sh.kind === 'addlink') {
    html = sheetFrame('Dodaj link', forPart, `<input id="al-url" class="in big mono" placeholder="Wklej adres PDF albo sklepu" value="${esc(sh.url || '')}">
      <button class="tool outline" data-act="al-paste">Wklej ze schowka</button>
      <div class="flabel2">To jest</div>
      ${segBtns([['datasheet_url', 'Datasheet'], ['shop_url', 'Sklep']], sh.linkKind, 'al-kind')}
      <button class="btn accent wide" data-act="save-addlink">Zapisz link</button>`);
  } else if (sh.kind === 'tech') {
    const d = sh.draft;
    const pk = [...PKGS.SMD, ...PKGS.THT].filter((x) => !d.mount || PKGS[d.mount].includes(x));
    html = sheetFrame('Dane techniczne', forPart, `<div class="flabel2">Producent</div>
      <input id="t-mfr" class="in" placeholder="np. Texas Instruments" value="${esc(d.manufacturer)}">
      <div class="flabel2">Numer katalogowy</div>
      <input id="t-mpn" class="in mono" placeholder="np. NE555P" value="${esc(d.mpn)}">
      <div class="flabel2">Montaż</div>
      ${segBtns([['SMD', 'SMD'], ['THT', 'THT']], d.mount, 't-mount')}
      <div class="flabel2">Obudowa</div>
      <input id="t-pkg" class="in mono" placeholder="wybierz poniżej albo wpisz" value="${esc(d.package)}">
      <div class="chips-wrap">${pk.map((x) => `<button class="fchip${d.package === x ? ' on' : ''}" data-act="t-pkg" data-id="${esc(x)}">${esc(x)}</button>`).join('')}</div>
      <div class="flabel2">Parametry</div>
      <textarea id="t-params" class="in" rows="3" placeholder="np. 50 V, X7R, tolerancja 10%">${esc(d.params)}</textarea>
      <button class="btn accent wide" data-act="save-tech">Zapisz</button>`);
  } else if (sh.kind === 'delete') {
    html = sheetFrame('Usunąć część?', forPart, `<div class="warn-box"><b>Tego nie da się cofnąć.</b> Część zniknie z magazynu razem ze zdjęciami, ilością i miejscem w pudełkach.</div>
      <div>Wpisz <b class="mono">${esc(p.name)}</b>, żeby potwierdzić</div>
      <input id="del-in" class="in mono danger-in" autocomplete="off">
      <button class="btn danger-fill wide" data-act="do-delete">Usuń na zawsze</button>
      <button class="btn ghost wide" data-act="sheet-close">Nie usuwaj</button>`);
  } else if (sh.kind === 'assign') {
    html = assignSheet(sh, p);
  } else if (sh.kind === 'thing') {
    html = thingSheet(sh);
  } else if (sh.kind === 'addtext') {
    html = textSheet(sh);
  } else if (sh.kind === 'fix') {
    html = fixSheet(sh);
  } else if (sh.kind === 'fixcat') {
    html = fixCatSheet(sh);
  }
  host.innerHTML = html;
  const first = host.querySelector('#ed-value, #al-url, #lk-ds, #add-text');
  if (first) setTimeout(() => first.focus(), 50);
  const catQ = $('cat-q');
  if (catQ) catQ.oninput = () => { sh.query = catQ.value; renderSheet(); const el = $('cat-q'); el.focus(); el.setSelectionRange(el.value.length, el.value.length); };
  const delIn = $('del-in');
  if (delIn) delIn.oninput = () => { host.querySelector('[data-act=do-delete]').disabled = norm(delIn.value.trim()) !== norm(p.name); };
  if (delIn) host.querySelector('[data-act=do-delete]').disabled = true;
}

/* --- arkusz „Przypisz pudełko” -------------------------------------- */

const RECENT_BOXES_KEY = 'wz-recent-boxes';

function rememberBox(id) {
  const list = [id, ...readJson(RECENT_BOXES_KEY, []).filter((x) => x !== id)].slice(0, 4);
  writeJson(RECENT_BOXES_KEY, list);
}

function assignSheet(sh, p) {
  const boxes = state.snap.boxes;
  const title = sh.stage === 'new' ? 'Nowe pudełko' : sh.target === 'sort' ? 'Wybierz pudełko' : sh.from != null ? 'Przenieś do' : 'Przypisz pudełko';
  const sub = sh.target === 'sort' ? 'Do niego trafią kolejne części' : sh.target === 'thing' ? 'Tu trafi część po zatwierdzeniu' : (p && sh.target === 'part' ? `dla: ${p.name}` : 'Karton › kuweta › przegroda');
  if (sh.stage === 'confirm') {
    const b = state.boxById.get(sh.picked);
    return sheetFrame(title, sub, `<div class="confirm-box">${b.photo ? `<div class="box-ph big stripes"${bg(b.photo)}></div>` : '<div class="box-ph big ph-dashed"></div>'}
      <div><div class="box-big">${esc(b.code)}</div><div class="box-path">${esc(b.path)}</div></div></div>
      <button class="btn accent wide tall" data-act="confirm-assign">Włóż do ${esc(b.code)}</button>
      <button class="btn ghost wide" data-act="assign-back">Wybierz inne pudełko</button>`);
  }
  if (sh.stage === 'new') {
    const parents = boxes.filter((b) => b.code.split('/').length < 3);
    return sheetFrame(title, '', `<div class="flabel2">W czym leży?</div>
      <div class="chips-wrap">${[[null, '— (w niczym, stoi samo)'], ...parents.map((b) => [b.id, b.code])].map(([v, l]) =>
        `<button class="fchip${sh.parent === v ? ' on' : ''}" data-act="new-parent" data-id="${v ?? ''}">${esc(l)}</button>`).join('')}</div>
      <div class="flabel2">Nazwa</div>
      <input id="new-name" class="in big" value="${esc(sh.name || '')}">
      <div class="code-row"><span>Kod na etykietę</span><b class="mono">${esc(sh.code || '…')}</b></div>
      <button class="btn accent wide" data-act="create-box">${sh.target === 'create' ? `Utwórz ${esc(sh.code || '')}` : `Utwórz i wybierz ${esc(sh.code || '')}`}</button>`);
  }
  const recent = readJson(RECENT_BOXES_KEY, []).map((id) => state.boxById.get(id)).filter(Boolean);
  const qn = norm(sh.query || '');
  const found = qn ? boxes.filter((b) => norm(`${b.code} ${b.name} ${b.code.replace(/\//g, '')}`).includes(qn)).slice(0, 8) : [];
  return sheetFrame(title, sub, `${recent.length ? `<div class="flabel2">Ostatnio używane</div><div class="grid2">${recent.map((b) => boxButton(b, 'pick-box')).join('')}</div>` : ''}
    ${!boxes.length ? '<div class="note-box">Nie masz jeszcze żadnego pudełka. Utwórz pierwsze poniżej — potem będzie tu na górze.</div>' : ''}
    ${boxes.length ? `<div class="search-in"><input id="box-q" placeholder="Szukaj pudełka po kodzie lub nazwie" value="${esc(sh.query || '')}"></div>
      ${qn ? `<div class="stack tight">${found.map((b) => `<button class="box-line" data-act="pick-box" data-id="${b.id}"><b class="mono">${esc(b.code)}</b><span>${esc(b.path)}</span></button>`).join('')}
        ${found.length ? '' : '<div class="hint2">Nie ma takiego pudełka. Utwórz nowe poniżej.</div>'}</div>` : ''}
      ${!qn && !recent.length ? `<div class="stack tight">${boxes.slice(0, 12).map((b) => `<button class="box-line" data-act="pick-box" data-id="${b.id}"><b class="mono">${esc(b.code)}</b><span>${esc(b.path)}</span></button>`).join('')}</div>` : ''}` : ''}
    <button class="tool dashed" data-act="new-box">+ Nowe pudełko</button>
    ${sh.target === 'thing' ? `${sh.current ? '<button class="tool outline" data-act="thing-box-clear">Bez pudełka</button>' : ''}
      <button class="btn ghost wide" data-act="thing-box-back">Wróć</button>` : ''}`);
}

/* Pudełko dla rzeczy w kolejce — podpowiedź do zapisu przy zatwierdzeniu (#77). */
async function setThingBox(iid, code) {
  const body = new FormData();
  body.append('text', code || '');
  try {
    const r = await fetch(`/api/intake/${iid}/location`, {method: 'POST', body});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    toast(code ? `Pudełko: ${code} ✓` : 'Bez pudełka ✓');
  } catch (err) { toast(`Nie zapisano: ${err.message}`, {bad: true}); }
  openSheet({kind: 'thing', iid});
  loadAdd();
}

async function startNewBox(sh, parent) {
  sh.stage = 'new';
  sh.parent = parent ?? null;
  try {
    const hint = await api('GET', `/api/warsztat/boxes/next${sh.parent != null ? `?parent=${sh.parent}` : ''}`);
    sh.name = hint.name;
    sh.code = hint.code;
  } catch (err) {
    sh.name = '';
    sh.code = '';
    toast(err.message, {bad: true});
  }
  if (state.sheet === sh) renderSheet();
}

async function assignTo(pid, boxId, from) {
  const box = state.boxById.get(boxId);
  rememberBox(boxId);
  return save(api('POST', `/api/warsztat/parts/${pid}/box`, {box: boxId, from: from ?? null}),
    `${box ? box.code : 'Pudełko'} ✓`,
    () => save(from != null
      ? api('POST', `/api/warsztat/parts/${pid}/box`, {box: from, from: boxId})
      : api('DELETE', `/api/warsztat/parts/${pid}/box/${boxId}`), 'Cofnięte'));
}

async function refreshBoxes(boxes) {
  state.snap.boxes = boxes.map((b) => ({...b, n: (state.boxById.get(b.id) || {}).n || 0}));
  useSnapshot(state.snap);
  writeJson(SNAP_KEY, state.snap);
}

/* --- akcje karty i arkuszy ------------------------------------------- */

const pickFile = () => new Promise((resolve) => {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.capture = 'environment';
  input.onchange = () => resolve(input.files && input.files[0]);
  input.click();
});

function applyTheme() {
  const pref = localStorage.getItem(THEME_KEY) || 'dark';
  const dark = pref === 'system' ? !window.matchMedia('(prefers-color-scheme: light)').matches : pref === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? '#262420' : '#f3f1ec';
}
window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', applyTheme);

const CARD_ACTIONS = {
  'open-settings': () => openSheet({kind: 'settings'}),
  'set-theme': (raw) => { localStorage.setItem(THEME_KEY, raw); applyTheme(); renderSheet(); },
  'set-cam': (raw) => { localStorage.setItem(PRECISE_KEY, raw); renderSheet(); },
  'sheet-close': () => closeSheet(),
  'toast-undo': () => { const o = state.toastOpts; $('toast').hidden = true; if (o && o.undo) o.undo(); },
  'toast-action': () => { const o = state.toastOpts; $('toast').hidden = true; if (o && o.action) o.action.fn(); },
  'photo-idx': (raw) => { state.photoIdx = Number(raw); renderCard(); },
  'photo-add': () => takePhoto({kind: 'part', pid: state.partId}),
  'photo-main': () => {
    const ph = (state.cards.get(state.partId) || {}).photos || [];
    const current = ph[Math.min(state.photoIdx, ph.length - 1)];
    if (!current) return;
    save(api('POST', `/api/warsztat/parts/${state.partId}/photos/${current.id}/main`), 'Ustawione jako główne ✓').then(() => { state.photoIdx = 0; renderCard(); });
  },
  'photo-remove': () => {
    const ph = (state.cards.get(state.partId) || {}).photos || [];
    const current = ph[Math.min(state.photoIdx, ph.length - 1)];
    if (!current || !confirm('Usunąć to zdjęcie z części?')) return;
    save(api('DELETE', `/api/warsztat/parts/${state.partId}/photos/${current.id}`), 'Zdjęcie usunięte').then(() => { state.photoIdx = 0; renderCard(); });
  },
  checked: () => {
    const id = state.partId;
    save(editPart(id, {checked: true}), 'Sprawdzone ✓', () => save(editPart(id, {checked: false}), 'Cofnięte'));
  },
  take: () => {
    const id = state.partId;
    const before = state.byId.get(id).q;
    save(api('POST', `/api/warsztat/parts/${id}/take`), `Zostało ${before.n - 1} szt.`,
      () => save(api('POST', `/api/warsztat/parts/${id}/quantity`, {kind: 'exact', n: before.n, low: state.byId.get(id).low}), 'Cofnięte'));
  },
  'ed-name': () => openSheet({kind: 'text', field: 'name', draft: state.byId.get(state.partId).name}),
  'ed-what': () => openSheet({kind: 'text', field: 'what', draft: (state.cards.get(state.partId) || {}).what || ''}),
  'ed-notes': () => openSheet({kind: 'text', field: 'notes', draft: (state.cards.get(state.partId) || {}).notes || ''}),
  'save-text': () => {
    const sh = state.sheet;
    const id = state.partId;
    const value = $('ed-value').value.trim();
    const card = state.cards.get(id) || {};
    const before = {name: state.byId.get(id).name, what: card.what || '', notes: card.notes || ''}[sh.field];
    closeSheet();
    save(editPart(id, {[sh.field]: value}), 'Zapisano ✓', () => save(editPart(id, {[sh.field]: before}), 'Cofnięte'));
  },
  'ed-qty': () => {
    const p = state.byId.get(state.partId);
    openSheet({kind: 'qty', draft: {k: p.q.k === 'none' ? 'exact' : p.q.k, n: p.q.n ?? (p.q.k === 'approx' ? 50 : 1), low: p.low}});
  },
  'qty-kind': (raw) => { const d = state.sheet.draft; d.k = raw; if (d.n == null) d.n = raw === 'approx' ? 50 : 1; renderSheet(); },
  'qty-dec': () => { const d = state.sheet.draft; d.n = Math.max(0, (d.n || 0) - (d.k === 'approx' ? 10 : 1)); renderSheet(); },
  'qty-inc': () => { const d = state.sheet.draft; d.n = (d.n || 0) + (d.k === 'approx' ? 10 : 1); renderSheet(); },
  'qty-low': () => { state.sheet.draft.low = !state.sheet.draft.low; renderSheet(); },
  'save-qty': () => {
    const id = state.partId;
    const d = state.sheet.draft;
    const p = state.byId.get(id);
    const before = {kind: p.q.k === 'none' ? 'some' : p.q.k, n: p.q.n, low: p.low};
    closeSheet();
    save(api('POST', `/api/warsztat/parts/${id}/quantity`, {kind: d.k, n: d.k === 'some' ? null : d.n, low: d.low}),
      `Zapisano ${qText({k: d.k, n: d.n})} ✓`, () => save(api('POST', `/api/warsztat/parts/${id}/quantity`, before), 'Cofnięte'));
  },
  'ed-cat': () => openSheet({kind: 'cat', query: ''}),
  'pick-cat': (raw) => {
    const id = state.partId;
    const before = state.byId.get(id).cat;
    const cat = state.catById.get(Number(raw));
    closeSheet();
    save(editPart(id, {category: Number(raw)}), `Kategoria: ${cat ? cat.name : ''} ✓`,
      () => save(editPart(id, {category: before}), 'Cofnięte')).then(() => loadSnapshotQuiet());
  },
  'ed-links': () => openSheet({kind: 'links', na: !!((state.cards.get(state.partId) || {}).edit || {}).datasheet_na}),
  'lk-na': () => { state.sheet.na = !state.sheet.na; renderSheet(); },
  'save-links': () => {
    const changes = {datasheet_na: state.sheet.na};
    const ds = $('lk-ds').value.trim();
    const shop = $('lk-shop').value.trim();
    if (ds) changes.datasheet_url = ds;
    if (shop) changes.shop_url = shop;
    closeSheet();
    save(editPart(state.partId, changes), 'Zapisano ✓');
  },
  'ed-addlink': () => openSheet({kind: 'addlink', url: '', linkKind: 'datasheet_url'}),
  'al-kind': (raw) => { state.sheet.url = $('al-url').value; state.sheet.linkKind = raw; renderSheet(); },
  'al-paste': async () => {
    try { state.sheet.url = await navigator.clipboard.readText(); renderSheet(); } catch { toast('Przeglądarka nie dała dostępu do schowka — wklej palcem.', {bad: true}); }
  },
  'save-addlink': () => {
    const url = $('al-url').value.trim();
    const kind = state.sheet.linkKind;
    if (!url) return;
    closeSheet();
    save(editPart(state.partId, {[kind]: url}), 'Link zapisany ✓');
  },
  'ed-tech': () => {
    const e = (state.cards.get(state.partId) || {}).edit || {};
    openSheet({kind: 'tech', draft: {manufacturer: e.manufacturer || '', mpn: e.mpn || '', mount: e.mount || state.byId.get(state.partId).m || '', package: e.package || '', params: e.params || ''}});
  },
  't-mount': (raw) => { readTech(); state.sheet.draft.mount = raw; renderSheet(); },
  't-pkg': (raw) => { readTech(); state.sheet.draft.package = raw; if (PKGS.SMD.includes(raw)) state.sheet.draft.mount = 'SMD'; if (PKGS.THT.includes(raw)) state.sheet.draft.mount = 'THT'; renderSheet(); },
  'save-tech': () => {
    readTech();
    const d = state.sheet.draft;
    closeSheet();
    save(editPart(state.partId, d), 'Zapisano ✓');
  },
  'ed-delete': () => openSheet({kind: 'delete'}),
  'do-delete': async () => {
    const id = state.partId;
    const p = state.byId.get(id);
    try {
      await api('POST', `/api/warsztat/parts/${id}/delete`, {confirm: $('del-in').value});
    } catch (err) { toast(`Nie usunięto: ${err.message}`, {bad: true}); return; }
    closeSheet();
    state.snap.parts = state.snap.parts.filter((x) => x.id !== id);
    useSnapshot(state.snap);
    writeJson(SNAP_KEY, state.snap);
    state.partId = null;
    toast(`Usunięto: ${p.name}`);
    render();
  },
  // pudełka
  assign: () => openSheet({kind: 'assign', target: 'part', stage: 'pick', pid: state.partId, query: ''}),
  'box-move': (raw) => openSheet({kind: 'assign', target: 'part', stage: 'pick', pid: state.partId, from: Number(raw), query: ''}),
  'box-remove': (raw) => {
    const id = state.partId;
    const boxId = Number(raw);
    const box = state.boxById.get(boxId);
    save(api('DELETE', `/api/warsztat/parts/${id}/box/${boxId}`), `Wyjęte z ${box ? box.code : 'pudełka'}`,
      () => save(api('POST', `/api/warsztat/parts/${id}/box`, {box: boxId}), 'Cofnięte'));
  },
  'pick-box': (raw) => {
    const sh = state.sheet;
    const boxId = Number(raw);
    if (sh.target === 'sort') { rememberBox(boxId); state.sortBox = boxId; closeSheet(); renderView(); return; }
    if (sh.target === 'thing') { rememberBox(boxId); setThingBox(sh.iid, (state.boxById.get(boxId) || {}).code); return; }
    sh.stage = 'confirm';
    sh.picked = boxId;
    renderSheet();
  },
  'assign-back': () => { state.sheet.stage = 'pick'; renderSheet(); },
  'confirm-assign': () => {
    const sh = state.sheet;
    closeSheet();
    assignTo(sh.pid, sh.picked, sh.from);
  },
  'new-box': () => startNewBox(state.sheet, null),
  'new-parent': (raw) => { const sh = state.sheet; sh.name = $('new-name').value; startNewBox(sh, raw === '' ? null : Number(raw)); },
  'create-box': async () => {
    const sh = state.sheet;
    const name = $('new-name').value.trim();
    let result;
    try {
      result = await api('POST', '/api/warsztat/boxes', {parent: sh.parent, name});
    } catch (err) { toast(`Nie utworzono: ${err.message}`, {bad: true}); return; }
    await refreshBoxes(result.boxes);
    const created = result.created;
    if (sh.target === 'create') {
      closeSheet();
      Object.assign(state, {tab: 'browse', browseMode: 'box', boxId: created});
      toast(`Utworzono ${(state.boxById.get(created) || {}).code || ''}`);
      render();
    } else if (sh.target === 'thing') {
      rememberBox(created);
      setThingBox(sh.iid, (state.boxById.get(created) || {}).code);
    } else if (sh.target === 'sort') {
      rememberBox(created);
      state.sortBox = created;
      closeSheet();
      renderView();
    } else {
      sh.stage = 'confirm';
      sh.picked = created;
      renderSheet();
    }
  },
  'new-top-box': () => { const sh = {kind: 'assign', target: 'create', stage: 'new'}; state.sheet = sh; startNewBox(sh, null); },
  'add-child-box': (raw) => { const sh = {kind: 'assign', target: 'create', stage: 'new'}; state.sheet = sh; startNewBox(sh, Number(raw)); },
  // tryb sortowania listy „Bez pudła”
  'start-sort': () => openSheet({kind: 'assign', target: 'sort', stage: 'pick', query: ''}),
  'stop-sort': () => { state.sortBox = null; renderView(); },
  'sort-into': (raw) => { state.sortBox = Number(raw); rememberBox(Number(raw)); Object.assign(state, {tab: 'lists', listId: 'nobox'}); render(); },
  'sort-put': (raw) => assignTo(Number(raw), state.sortBox),
};

function readTech() {
  const d = state.sheet.draft;
  d.manufacturer = $('t-mfr').value;
  d.mpn = $('t-mpn').value;
  d.package = $('t-pkg').value;
  d.params = $('t-params').value;
}

/* Po zmianie kategorii liczniki kafelków są nieaktualne — dociągamy zrzut
   w tle, bez blokowania ekranu. */
function loadSnapshotQuiet() { setTimeout(loadSnapshot, 300); }

document.addEventListener('input', (event) => {
  if (event.target.id === 'box-q' && state.sheet) {
    state.sheet.query = event.target.value;
    const pos = event.target.value.length;
    renderSheet();
    const el = $('box-q');
    if (el) { el.focus(); el.setSelectionRange(pos, pos); }
  }
});

/* --- Dodaj (projekt „Warsztat v2”) ------------------------------------

   Kolejka intake'u w układzie „co mam teraz zrobić”, a nie w zakładkach
   według stanu bazy:
     1. Do zatwierdzenia — propozycje agenta (jedyne, co czeka na mnie),
     2. Do dokończenia  — zdjęcia, których jeszcze nie zleciłem, i te,
                          które czekają w telefonie na zasięg,
     3. Rozpoznaje agent — zlecone, czekają na przebieg automatu,
     4. Gotowe dziś     — co dziś weszło do magazynu.
   Pusta sekcja znika.

   Wysyłka i poczekalnia bez zasięgu działają tak samo jak w dotychczasowym
   ekranie (`outbox.js`): zdjęcia nie gubimy nigdy, a błąd trwały (zły
   format) odróżniamy od chwilowego (brak zasięgu). */

const ZOOM_KEY = 'bom.camera_zoom'; // wspólny z dotychczasowym ekranem
const add = {
  items: [], proposals: [], waiting: [], loaded: false, flushing: false,
  saving: new Set(), errors: new Map(), rejecting: null, noteOpen: new Set(),
  doneOpen: false, queuedOpen: false, history: false, selProp: null,
};

class UploadError extends Error {
  constructor(message, permanent) { super(message); this.permanent = Boolean(permanent); }
}

async function loadAdd() {
  try {
    add.waiting = await Outbox.all();
  } catch { add.waiting = []; }
  try {
    const [items, props] = await Promise.all([
      fetch('/api/intake?limit=200', {cache: 'no-store'}).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status)))),
      fetch('/api/proposals?status=pending', {cache: 'no-store'}).then((r) => (r.ok ? r.json() : {proposals: []})),
    ]);
    add.items = items.items || [];
    add.proposals = props.proposals || [];
    add.loaded = true;
  } catch {
    add.loaded = true;
  }
  if (add.selProp && !add.proposals.some((p) => p.id === add.selProp)) add.selProp = null;
  if (!add.selProp && add.proposals.length) add.selProp = add.proposals[0].id;
  renderTabs();
  if (state.tab === 'add') { state.keepScroll = true; renderView(); renderAddRight(); }
  if (state.sheet && state.sheet.kind === 'thing') renderSheet();
}

async function uploadPhoto(file, parentId, name) {
  const body = new FormData();
  body.append('photo', file, name || file.name || 'zdjecie.jpg');
  if (parentId) body.append('parent_id', String(parentId));
  let response;
  try {
    response = await fetch('/api/intake/photo', {method: 'POST', body});
  } catch (err) {
    throw new UploadError(err.message || 'brak połączenia', false);
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { const data = await response.json(); detail = data.detail || data.error || detail; } catch { /* bez JSON-a */ }
    const permanent = response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429;
    throw new UploadError(detail, permanent);
  }
  return response.json();
}

/* Wysłanie zdjęć; to, co nie poszło, ląduje w poczekalni z powodem. */
async function sendPhotos(files, parentId) {
  const list = [...(files || [])];
  if (!list.length) { toast('Nie wybrano żadnego zdjęcia', {bad: true}); return []; }
  const sent = [];
  for (const file of list) {
    try {
      if (!file.size) throw new UploadError('plik jest pusty — jeśli zdjęcie jest tylko w chmurze, pobierz je najpierw na telefon', true);
      sent.push(await uploadPhoto(file, parentId));
    } catch (err) {
      try {
        await Outbox.add({blob: file, name: file.name, locationHint: '', parentId: parentId || null, note: String(err.message || err), blocked: err.permanent});
        toast(err.permanent ? `Odrzucone: ${err.message}` : 'Brak zasięgu — zdjęcie czeka w telefonie, wyślę, gdy wróci sieć', {bad: true});
      } catch (storeError) {
        toast(`NIE ZAPISANO: ${err.message}. Poczekalnia niedostępna (${storeError.message}) — zrób zdjęcie ponownie przy zasięgu.`, {bad: true});
      }
    }
  }
  if (sent.length && navigator.vibrate) navigator.vibrate(30);
  if (sent.length) flushOutbox();
  await loadAdd();
  return sent;
}

async function flushOutbox() {
  if (add.flushing || !navigator.onLine) return;
  add.flushing = true;
  let sent = 0;
  try {
    for (const entry of await Outbox.all()) {
      if (entry.blocked) continue;
      try {
        await uploadPhoto(entry.blob, entry.parentId, entry.name);
        await Outbox.remove(entry.id);
        sent += 1;
      } catch (err) {
        if (err.permanent && entry.parentId) {
          try { await uploadPhoto(entry.blob, null, entry.name); await Outbox.remove(entry.id); sent += 1; continue; } catch (again) { err = again; }
        }
        await Outbox.update(entry.id, {note: String(err.message || err), blocked: err.permanent});
        if (!err.permanent) break;
      }
    }
  } catch { /* poczekalnia niedostępna */ } finally { add.flushing = false; }
  if (sent) { toast(sent === 1 ? 'Zaległe zdjęcie wysłane' : `Wysłano ${sent} zaległych zdjęć`); loadAdd(); }
}

/* --- aparat ------------------------------------------------------------ */

const pickPhotos = (multiple, camera) => new Promise((resolve) => {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  if (multiple) input.multiple = true;
  if (camera) input.capture = 'environment';
  // Pole w DOM-ie: Chrome na Androidzie potrafi nie otworzyć wybieraka
  // dla pola, którego nie ma w dokumencie (lekcja z intake'u).
  input.className = 'sr-only-file';
  input.onchange = () => { resolve([...(input.files || [])]); input.remove(); };
  document.body.append(input);
  input.click();
});

let camStream = null;
let camTrack = null;

async function openCamera(purpose) {
  try {
    camStream = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: {ideal: 'environment'}, width: {ideal: 4096}, height: {ideal: 3072}}, audio: false});
  } catch (err) {
    toast(`Aparat w aplikacji niedostępny: ${err.message}. Przełącz w Ustawieniach na zwykły.`, {bad: true});
    return;
  }
  camTrack = camStream.getVideoTracks()[0];
  const caps = camTrack.getCapabilities ? camTrack.getCapabilities() : {};
  const zoom = caps.zoom || null;
  const levels = zoom ? [...new Set([zoom.min, Math.min(2, zoom.max), Math.min(3, zoom.max)])] : [];
  const el = $('camera');
  el.hidden = false;
  el.innerHTML = `<div class="cam-top"><span>Aparat precyzyjny</span><button data-act="cam-cancel">Anuluj</button></div>
    <div class="cam-view"><video id="cam-video" playsinline autoplay muted></video></div>
    <div class="cam-zoom" id="cam-zoom">${zoom ? levels.map((z) => `<button data-act="cam-zoom" data-id="${z}">${z}×</button>`).join('') : '<span>ten aparat nie pozwala ustawić zoomu</span>'}</div>
    <div class="cam-hint">${zoom ? 'Zoom zapamiętany na następne zdjęcia' : ''}</div>
    <div class="cam-shoot"><button data-act="cam-shutter">Zrób zdjęcie</button></div>`;
  $('cam-video').srcObject = camStream;
  el.dataset.purpose = JSON.stringify(purpose);
  if (zoom) {
    const saved = Number(localStorage.getItem(ZOOM_KEY) || zoom.min);
    await setZoom(Math.min(Math.max(saved, zoom.min), zoom.max));
  }
}

async function setZoom(z) {
  if (!camTrack) return;
  try {
    await camTrack.applyConstraints({advanced: [{zoom: z}]});
    localStorage.setItem(ZOOM_KEY, String(z));
    for (const b of document.querySelectorAll('#cam-zoom button')) b.classList.toggle('on', Number(b.dataset.id) === Number(z));
  } catch { /* aparat nie przyjął zoomu */ }
}

function closeCamera() {
  if (camStream) for (const track of camStream.getTracks()) track.stop();
  camStream = null; camTrack = null;
  $('camera').hidden = true; $('camera').innerHTML = '';
}

async function shutter() {
  if (!camTrack) return;
  let blob = null;
  if (window.ImageCapture) {
    try {
      const capture = new ImageCapture(camTrack);
      const caps = await capture.getPhotoCapabilities();
      blob = await capture.takePhoto({imageWidth: caps.imageWidth ? caps.imageWidth.max : undefined, imageHeight: caps.imageHeight ? caps.imageHeight.max : undefined});
    } catch { /* zostaje klatka podglądu */ }
  }
  if (!blob) {
    const video = $('cam-video');
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.92));
  }
  const purpose = JSON.parse($('camera').dataset.purpose || '{}');
  closeCamera();
  if (!blob) { toast('Nie udało się zrobić zdjęcia', {bad: true}); return; }
  handleShot([new File([blob], 'precyzyjne.jpg', {type: 'image/jpeg'})], purpose);
}

/* Co zrobić ze zdjęciem: nowa rzecz, kolejne ujęcie, zdjęcie części. */
async function handleShot(files, purpose) {
  if (purpose.kind === 'part') return uploadPartPhoto(files[0], purpose.pid);
  if (purpose.kind === 'shot') {
    const sent = await sendPhotos(files, purpose.iid);
    if (sent.length) toast('Ujęcie dodane ✓');
    return;
  }
  const sent = await sendPhotos(files);
  if (sent.length === 1) openSheet({kind: 'thing', iid: sent[0].id});
  else if (sent.length > 1) toast(`${sent.length} zdjęcia w „Do dokończenia”`);
}

async function takePhoto(purpose) {
  if (localStorage.getItem(PRECISE_KEY) === '1') return openCamera(purpose);
  const files = await pickPhotos(false, true);
  if (files.length) handleShot(files, purpose);
}

async function uploadPartPhoto(file, pid) {
  const form = new FormData();
  form.append('photo', file, file.name || 'zdjecie.jpg');
  toast('Wysyłam zdjęcie…');
  const card = await save(api('POST', `/api/warsztat/parts/${pid}/photos`, form), 'Zdjęcie dodane ✓');
  if (card) { state.photoIdx = Math.max(0, (card.photos || []).length - 1); renderCard(); }
}

/* --- widok -------------------------------------------------------------- */

const isToday = (iso) => iso && new Date(iso).toDateString() === new Date().toDateString();
const small = (url, edge = 160) => (url ? `${url}${url.includes('?') ? '&' : '?'}w=${edge}` : '');

function propQty(payload) {
  const n = payload.amount;
  if (payload.instock_unknown) return n ? {k: 'approx', n} : {k: 'some', n: null};
  return {k: 'exact', n: n ?? 1};
}

function sections() {
  const proposals = add.proposals;
  const waitingIds = new Set(proposals.map((p) => p.item_id));
  const pending = add.items.filter((i) => i.status === 'pending' && !waitingIds.has(i.id));
  return {
    proposals,
    todo: pending.filter((i) => !i.recognize_requested_at),
    queued: pending.filter((i) => i.recognize_requested_at),
    done: add.items.filter((i) => i.status === 'committed' && (add.history || isToday(i.processed_at))),
  };
}

function proposalCard(p, size) {
  const q = propQty(p.payload);
  const saving = add.saving.has(p.id);
  const error = add.errors.get(p.id);
  const verdict = p.action === 'merge' && p.existing
    ? {dot: 'var(--accent)', head: 'Dołoży sztuki do:', rest: p.existing.name}
    : {dot: 'var(--ok)', head: 'Nowa część', rest: 'w magazynie jeszcze jej nie ma'};
  const note = p.payload.note || '';
  const open = add.noteOpen.has(p.id);
  const img = p.photo_url ? small(p.photo_url, size === 'large' ? 1024 : 480) : '';
  const sel = DESK.matches && add.selProp === p.id && size !== 'large' ? ' sel' : '';
  if (size === 'compact') {
    // Komputer: w lewej kolumnie zwarty wiersz, pełna karta jest po prawej.
    return `<div class="pcard compact${sel}" data-act="prop-select" data-id="${p.id}"><div class="pcard-top">
      <div class="thumb stripes"${bg(small(p.photo_url))}></div>
      <div class="pcard-info"><div class="pcard-name small">${esc(p.payload.name)}</div>
        <div class="chips">${qtyChip({q})}${p.payload.needs_review ? '<span class="chip chk">? do sprawdzenia</span>' : ''}${saving ? '<span class="chip">zapisuję…</span>' : ''}${error ? '<span class="chip low">nie zapisano</span>' : ''}</div>
        <div class="pcard-cat">${esc(verdict.head)} ${esc(verdict.rest)}</div></div></div></div>`;
  }
  return `<div class="pcard ${size}${sel}" data-act="${size === 'large' ? '' : 'prop-select'}" data-id="${p.id}">
    <div class="pcard-top"><div class="pcard-img stripes"${bg(img)}></div>
      <div class="pcard-info"><div class="pcard-name">${esc(p.payload.name)}</div>
        <div class="chips">${qtyChip({q})}</div><div class="pcard-cat">${esc(p.payload.category || 'bez kategorii')}</div></div></div>
    <div class="verdict"><span class="vdot" style="background:${verdict.dot}"></span><span><b>${verdict.head}</b> ${esc(verdict.rest)}</span></div>
    ${p.payload.needs_review ? '<span class="chip chk" style="align-self:flex-start">? do sprawdzenia</span>' : ''}
    ${note ? `<button class="pnote${open ? ' open' : ''}" data-act="prop-note" data-id="${p.id}"><b>Uwaga agenta:</b><span>${esc(note)}</span><i>${open ? 'zwiń' : 'więcej'}</i></button>` : ''}
    ${error ? `<div class="perr"><b>Nie udało się zapisać.</b> ${esc(error)}</div>` : ''}
    ${saving ? '<div class="psaving">Zapisuję w magazynie…</div>'
      : add.rejecting === p.id
        ? `<div class="preject"><b>Odrzucić tę propozycję?</b><span>Nic nie trafi do magazynu. Zdjęcie zostanie w kolejce do ponownego rozpoznania.</span>
          <div class="grid2"><button class="tool outline" data-act="prop-reject-no">Nie, zostaw</button><button class="btn danger-fill" data-act="prop-reject-yes" data-id="${p.id}">Tak, odrzuć</button></div></div>`
        : `<button class="btn approve" data-act="prop-approve" data-id="${p.id}">${error ? 'Spróbuj ponownie' : 'Zatwierdź'}</button>
          <div class="pactions"><button class="tool outline" data-act="prop-fix" data-id="${p.id}">Popraw</button>
            <button class="ghost-small" data-act="prop-reject" data-id="${p.id}">Odrzuć</button></div>`}
  </div>`;
}

function itemTitle(i) {
  if (i.kind !== 'photo') return (i.payload_text || 'opis').split('\n')[0].slice(0, 60);
  const shots = (i.shots || []).length || 1;
  return shots > 1 ? `Zdjęcie · ${shots} ujęcia` : 'Zdjęcie';
}

function timeLabel(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isToday(iso) ? d.toLocaleTimeString('pl-PL', {hour: '2-digit', minute: '2-digit'}) : d.toLocaleDateString('pl-PL');
}

function addView() {
  const s = sections();
  const head = `<div class="title-bar"><div class="page-title">Dodaj części</div>${gearButton()}</div>
    <div class="grid2"><button class="add-big accent" data-act="add-photo">${'<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="6.5" width="18" height="13" rx="3"/><circle cx="12" cy="13" r="3.6"/></svg>'}Zdjęcie</button>
      <button class="add-big" data-act="add-text"><span class="lines"><i></i><i></i><i></i></span>Opisz tekstem</button></div>
    <button class="tool dashed" data-act="add-gallery">Z galerii — kilka naraz</button>`;
  if (!add.loaded) return `<div class="scroll add-scroll">${head}${skeletons()}</div>`;

  const groups = new Map();
  for (const p of s.proposals) { if (!groups.has(p.item_id)) groups.set(p.item_id, []); groups.get(p.item_id).push(p); }
  const propsHtml = [...groups.entries()].map(([itemId, props]) => {
    const item = add.items.find((i) => i.id === itemId) || {};
    const header = props.length > 1 ? `<div class="pgroup"><div class="pgroup-top"><div class="thumb stripes"${bg(small(item.photo_url))}></div>
        <div><b>Jedno zdjęcie · ${props.length} ${plural(props.length, 'propozycja', 'propozycje', 'propozycji')}</b><small class="mono">${esc(timeLabel(item.created_at))}</small></div></div>
        <button class="btn approve" data-act="prop-approve-all" data-id="${itemId}">Zatwierdź wszystkie (${props.length})</button></div>` : '';
    return `<div class="stack">${header}${props.map((p) => proposalCard(p, DESK.matches ? 'compact' : 'small')).join('')}</div>`;
  }).join('');

  const waitingRows = add.waiting.map((w) => `<div class="todo-row"><div class="thumb ph-dashed nophoto">czeka</div>
    <div class="todo-body"><b>Zdjęcie w telefonie</b><span class="wait"><i class="dot" style="background:var(--bad)"></i>${w.blocked ? `odrzucone: ${esc(w.note || '')}` : 'wyślę, gdy wróci sieć'}</span></div>
    ${w.blocked ? `<button class="tool outline" data-act="outbox-drop" data-id="${w.id}">Wyrzuć</button>` : `<button class="tool outline" data-act="outbox-retry">Ponów</button>`}</div>`).join('');
  const todoRows = s.todo.map((i) => `<div class="todo-row" data-act="open-thing" data-id="${i.id}">
    ${i.kind === 'photo' ? `<div class="thumb stripes"${bg(small(i.photo_url))}>${(i.shots || []).length > 1 ? `<span class="shots">${i.shots.length}</span>` : ''}</div>` : '<div class="thumb ph-dashed nophoto">tekst</div>'}
    <div class="todo-body"><b>${esc(itemTitle(i))}</b><span>${esc([timeLabel(i.created_at), i.kind === 'photo' && i.payload_text ? `„${i.payload_text}”` : ''].filter(Boolean).join(' · '))}</span></div>
    <button class="btn accent order" data-act="order" data-id="${i.id}">Rozpoznaj</button></div>`).join('');

  // Zlecone też da się przejrzeć i poprawić (ujęcie, notatka), dopóki
  // agent ich nie wziął — wcześniej widać było tylko licznik.
  const queuedRows = s.queued.map((i) => `<div class="todo-row" data-act="open-thing" data-id="${i.id}">
    ${i.kind === 'photo' ? `<div class="thumb stripes"${bg(small(i.photo_url))}>${(i.shots || []).length > 1 ? `<span class="shots">${i.shots.length}</span>` : ''}</div>` : '<div class="thumb ph-dashed nophoto">tekst</div>'}
    <div class="todo-body"><b>${esc(itemTitle(i))}</b><span>${esc([`zlecone ${timeLabel(i.recognize_requested_at)}`, i.kind === 'photo' && i.payload_text ? `„${i.payload_text}”` : ''].filter(Boolean).join(' · '))}</span></div>
    <span class="chev">›</span></div>`).join('');

  const todoCount = s.todo.length + add.waiting.length;
  const empty = !s.proposals.length && !todoCount && !s.queued.length;
  return `<div class="scroll add-scroll">${head}
    ${empty ? `<div class="empty"><div class="ph-dashed"></div><div class="empty-title">Nic nie czeka</div>
      <div class="empty-text">Zrób zdjęcie części albo kilku naraz. Agent rozpozna je w tle — nie musisz czekać przed ekranem.</div></div>` : ''}
    ${s.proposals.length ? `<div class="label gap">Do zatwierdzenia · ${s.proposals.length}</div><div class="stack">${propsHtml}</div>` : ''}
    ${todoCount ? `<div class="label gap">Do dokończenia · ${todoCount}</div><div class="stack">${waitingRows}${todoRows}</div>` : ''}
    ${s.queued.length ? `<div class="label gap">Rozpoznaje agent · ${s.queued.length}</div>
      <button class="queued" data-act="queued-toggle"><span class="ring"></span><div><b>${s.queued.length} ${plural(s.queued.length, 'rzecz czeka', 'rzeczy czekają', 'rzeczy czeka')} na agenta</b>
        <span>Rozpoznanie co godzinę. Propozycje pojawią się na górze listy.</span></div><i>${add.queuedOpen ? 'Zwiń' : 'Pokaż'}</i></button>
      ${add.queuedOpen ? `<div class="stack">${queuedRows}</div>` : ''}` : ''}
    ${s.done.length || add.history ? `<button class="done-toggle" data-act="done-toggle">${add.history ? 'Historia' : 'Gotowe dziś'} · ${s.done.length}<b>${add.doneOpen ? 'Zwiń' : 'Pokaż'}</b></button>
      ${add.doneOpen ? `<div class="done-list">${s.done.flatMap((i) => (i.created_parts && i.created_parts.length ? i.created_parts : (i.result || []))
        .map((r) => `<div class="done-row"${r.part_id ? ` data-act="done-part" data-id="${r.part_id}"` : ''}><b>✓</b><span>${esc(r.name)}</span></div>`)).join('')}
        ${add.history ? '' : '<button class="link-btn" data-act="history">Historia ›</button>'}</div>` : ''}` : ''}
  </div>`;
}

/* Komputer: zaznaczona propozycja w dużym rozmiarze po prawej. */
function renderAddRight() {
  const pane = $('add-right');
  if (!DESK.matches || state.tab !== 'add') { pane.hidden = true; return; }
  pane.hidden = false;
  $('desk-empty').hidden = true;
  $('card-pane').hidden = true;
  const p = add.proposals.find((x) => x.id === add.selProp);
  if (!p) {
    pane.innerHTML = '<div class="desk-empty" style="display:flex"><div class="ph-dashed big"></div><div class="desk-empty-title">Nic nie czeka na zatwierdzenie</div><div>Zrób zdjęcie albo wybierz rzecz z „Do dokończenia”.</div></div>';
    return;
  }
  const index = add.proposals.indexOf(p);
  pane.innerHTML = `<div class="add-right-inner"><div class="add-right-meta"><span>Propozycja ${index + 1} z ${add.proposals.length}</span>
    <span class="mono">Enter — Zatwierdź · ↑ ↓ — poprzednia / następna</span></div>${proposalCard(p, 'large')}</div>`;
}

/* --- arkusze Dodaj ---------------------------------------------------------- */

function thingSheet(sh) {
  const item = add.items.find((i) => i.id === sh.iid);
  if (!item) return sheetFrame('Ta rzecz', '', '<div class="muted">Wczytuję…</div>');
  const shots = item.shots && item.shots.length ? item.shots : [{url: item.photo_url}];
  const note = item.payload_text || '';
  const hint = item.location_hint || '';
  const hintBox = hint && state.snap ? state.snap.boxes.find((b) => b.code === hint) : null;
  return sheetFrame('Ta rzecz', timeLabel(item.created_at), `<div class="thing-photo stripes"${bg(small(shots[0].url, 1024))}>
      <span class="chip">${shots.length} ${plural(shots.length, 'ujęcie', 'ujęcia', 'ujęć')}</span></div>
    <div class="strip">${shots.map((s) => `<div class="strip-th"${bg(small(s.url))}></div>`).join('')}
      <button class="add-shot" data-act="thing-shot">+ Dodaj ujęcie</button></div>
    <div class="hint2">Spód modułu, zbliżenie na napis — każde ujęcie pomaga.</div>
    ${sh.editingNote ? `<textarea id="thing-note" class="in big" rows="3" placeholder="np. „10 sztuk”, „to jest BC557”">${esc(note)}</textarea>
      <button class="tool outline" data-act="thing-note-save">Zapisz notatkę</button>`
      : note ? `<div class="note-line"><div><small>Notatka</small><span>${esc(note)}</span></div><button class="tool outline" data-act="thing-note">Zmień</button></div>`
      : `<button class="tool outline" data-act="thing-note">Dopisz notatkę</button>
        <div class="hint2">„10 sztuk”, „to jest BC557” — notatka jest pewniejsza niż zdjęcie.</div>`}
    ${hint ? `<div class="note-line"><div><small>Pudełko</small><span>${esc(hintBox ? `${hintBox.code} · ${hintBox.path}` : hint)}</span></div><button class="tool outline" data-act="thing-box">Zmień</button></div>`
      : `<button class="tool outline" data-act="thing-box">Wskaż pudełko</button>
        <div class="hint2">Po zatwierdzeniu część od razu trafi do tego pudełka.</div>`}
    ${item.recognize_requested_at
      ? `<div class="hint2" style="text-align:center">Zlecone ${esc(timeLabel(item.recognize_requested_at))} — agent rozpozna to przy najbliższym przebiegu. Poprawki zapisane teraz weźmie pod uwagę.</div>
        <button class="btn accent wide tall" data-act="sheet-close">Gotowe</button>
        <button class="tool outline" data-act="thing-unorder">Wstrzymaj — wróci do „Do dokończenia”</button>`
      : `<button class="btn accent wide tall" data-act="thing-order">Gotowe — rozpoznaj</button>
        <div class="hint2" style="text-align:center">Agent rozpozna to w ciągu godziny.</div>
        <button class="tool outline" data-act="sheet-close">Zostaw na później</button>`}
    <button class="ghost-small" data-act="thing-discard">Wyrzuć tę rzecz</button>`);
}

function textSheet(sh) {
  const items = (sh.text || '').split(/[\n,;]+/).map((x) => x.trim()).filter(Boolean);
  return sheetFrame('Opisz tekstem', 'Lista części — każda linia to osobna rzecz', `<textarea id="add-text" class="in big mono" rows="5" placeholder="np. BC557 10 szt, BC547 20 szt">${esc(sh.text || '')}</textarea>
    <div class="hint2">Jedna rzecz w linii albo po przecinku. Ilość po nazwie.</div>
    ${items.length ? `<div class="flabel2">${items.length} ${plural(items.length, 'rzecz', 'rzeczy', 'rzeczy')} do rozpoznania</div>
      <div class="chips-wrap">${items.map((x) => `<span class="chip">${esc(x)}</span>`).join('')}</div>` : ''}
    <button class="btn accent wide tall" data-act="text-send" ${items.length ? '' : 'disabled'}>Gotowe — rozpoznaj</button>
    <button class="btn ghost wide" data-act="sheet-close">Anuluj</button>`);
}

function fixSheet(sh) {
  const p = add.proposals.find((x) => x.id === sh.pid);
  if (!p) return sheetFrame('Popraw propozycję', '', '<div class="muted">Tej propozycji już nie ma.</div>');
  const d = sh.draft;
  return sheetFrame('Popraw propozycję', '', `<div class="fix-top"><div class="thumb stripes"${bg(small(p.photo_url))}></div><small class="mono">wycinek ze zdjęcia #${p.item_id}</small></div>
    <div class="flabel2">Nazwa</div><input id="fx-name" class="in" value="${esc(d.name)}">
    <div class="flabel2">Ilość</div>
    ${segBtns([['exact', 'Dokładnie'], ['approx', 'Mniej więcej'], ['some', 'Jest']], d.k, 'fx-kind')}
    ${d.k !== 'some' ? `<div class="stepper"><button data-act="fx-dec">−</button><div><b class="mono">${d.k === 'approx' ? '~' : ''}${d.n}</b></div><button data-act="fx-inc">+</button></div>` : ''}
    <div class="flabel2">Kategoria</div>
    <button class="pick-row" data-act="fx-cat"><span>${esc(d.category || 'bez kategorii')}</span><b>Zmień</b></button>
    ${p.payload.note ? `<div class="note-line"><div><small>Uwaga agenta</small><span>${esc(p.payload.note)}</span></div></div>` : ''}
    <div class="hint2">Czy to ta sama część co istniejąca, rozstrzyga agent przy rozpoznaniu — zmiana nazwy unieważnia jego werdykt.</div>
    <div class="fix-actions"><button class="tool outline" data-act="fx-save">Zapisz</button><button class="btn approve" data-act="fx-save-approve">Zapisz i zatwierdź</button></div>`);
}

function fixCatSheet(sh) {
  const qn = norm(sh.query || '');
  const cats = [...state.snap.categories].sort((a, b) => a.name.localeCompare(b.name, 'pl')).filter((c) => !qn || norm(c.name).includes(qn));
  return sheetFrame('Kategoria', 'dla propozycji', `<div class="search-in"><input id="cat-q" placeholder="Zawęź listę" value="${esc(sh.query || '')}"></div>
    <div class="stack tight">${cats.map((c) => `<button class="cat-opt${c.name === sh.draft.category ? ' on' : ''}" data-act="fx-pick-cat" data-id="${esc(c.name)}"><span>${esc(c.name)}</span><small class="mono">${c.total}</small><b>${c.name === sh.draft.category ? '✓' : ''}</b></button>`).join('')}</div>`);
}

/* --- akcje Dodaj ------------------------------------------------------------- */

async function approveProposal(id) {
  if (add.saving.has(id)) return;
  add.saving.add(id); add.errors.delete(id); add.rejecting = null;
  redrawAdd();
  const p = add.proposals.find((x) => x.id === id);
  try {
    const result = await api('POST', `/api/proposals/${id}/approve`);
    add.saving.delete(id);
    const partRef = (result.created || []).find((ref) => /\/parts\/\d+$/.test(ref));
    const partId = partRef ? Number(partRef.split('/').pop()) : null;
    await loadAdd();
    const name = p ? p.payload.name : 'część';
    toast(p && p.action === 'merge' ? `Dołożono do: ${p.existing ? p.existing.name : name} ✓` : `Dodano: ${name} ✓`,
      partId ? {action: {label: 'Przypisz pudełko', fn: () => assignNewPart(partId)}} : {});
    loadSnapshotQuiet();
  } catch (err) {
    add.saving.delete(id);
    add.errors.set(id, err.message);
    redrawAdd();
  }
}

async function assignNewPart(partId) {
  try {
    const card = await api('GET', `/api/warsztat/parts/${partId}`);
    applyCard(card);
    openSheet({kind: 'assign', target: 'part', stage: 'pick', pid: partId, query: ''});
  } catch (err) { toast(err.message, {bad: true}); }
}

function redrawAdd() {
  if (state.tab !== 'add') return;
  state.keepScroll = true;
  renderView();
  renderAddRight();
}

Object.assign(CARD_ACTIONS, {
  'add-photo': () => takePhoto({kind: 'new'}),
  'add-gallery': async () => { const files = await pickPhotos(true, false); if (files.length) handleShot(files, {kind: 'new'}); },
  'add-text': () => openSheet({kind: 'addtext', text: ''}),
  'text-send': async () => {
    const text = $('add-text').value.trim();
    if (!text) return;
    const body = new FormData();
    body.append('text', text);
    try {
      const response = await fetch('/api/intake/voice', {method: 'POST', body});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      closeSheet();
      toast('Zlecone ✓ · agent rozpozna w ciągu godziny');
    } catch (err) { toast(`Nie wysłano: ${err.message}`, {bad: true}); }
    loadAdd();
  },
  'open-thing': (raw) => openSheet({kind: 'thing', iid: Number(raw)}),
  'thing-shot': () => takePhoto({kind: 'shot', iid: state.sheet.iid}),
  'thing-note': () => { state.sheet.editingNote = true; renderSheet(); setTimeout(() => $('thing-note') && $('thing-note').focus(), 50); },
  'thing-note-save': async () => {
    const sh = state.sheet;
    const body = new FormData();
    body.append('text', $('thing-note').value.trim());
    try {
      const r = await fetch(`/api/intake/${sh.iid}/note`, {method: 'POST', body});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      sh.editingNote = false;
      toast('Notatka zapisana ✓');
    } catch (err) { toast(`Nie zapisano: ${err.message}`, {bad: true}); }
    loadAdd();
  },
  'thing-box': () => {
    const iid = state.sheet.iid;
    const item = add.items.find((i) => i.id === iid);
    openSheet({kind: 'assign', target: 'thing', stage: 'pick', iid, current: item ? item.location_hint : null, query: ''});
  },
  'thing-box-clear': () => setThingBox(state.sheet.iid, ''),
  'thing-box-back': () => openSheet({kind: 'thing', iid: state.sheet.iid}),
  'thing-order': () => { const id = state.sheet.iid; closeSheet(); CARD_ACTIONS.order(String(id)); },
  'thing-unorder': async () => {
    const id = state.sheet.iid;
    try {
      await api('POST', `/api/intake/${id}/recognize?wanted=false`);
      closeSheet();
      toast('Wstrzymane · jest w „Do dokończenia”');
    } catch (err) { toast(`Nie wstrzymano: ${err.message}`, {bad: true}); }
    loadAdd();
  },
  'thing-discard': async () => {
    const id = state.sheet.iid;
    if (!confirm('Wyrzucić tę rzecz z kolejki razem ze zdjęciami?')) return;
    try { await api('DELETE', `/api/intake/${id}`); closeSheet(); toast('Wyrzucone'); } catch (err) { toast(err.message, {bad: true}); }
    loadAdd();
  },
  order: async (raw) => {
    try {
      await api('POST', `/api/intake/${Number(raw)}/recognize`);
      toast('Zlecone ✓ · agent rozpozna w ciągu godziny');
    } catch (err) { toast(`Nie zlecono: ${err.message}`, {bad: true}); }
    loadAdd();
  },
  'outbox-retry': () => flushOutbox(),
  'outbox-drop': async (raw) => { await Outbox.remove(Number(raw)); loadAdd(); },
  'prop-select': (raw) => { add.selProp = Number(raw); redrawAdd(); },
  'prop-note': (raw) => { const id = Number(raw); if (add.noteOpen.has(id)) add.noteOpen.delete(id); else add.noteOpen.add(id); redrawAdd(); },
  'prop-approve': (raw) => approveProposal(Number(raw)),
  'prop-approve-all': async (raw) => {
    const itemId = Number(raw);
    const ids = add.proposals.filter((p) => p.item_id === itemId).map((p) => p.id);
    ids.forEach((id) => add.saving.add(id));
    redrawAdd();
    try {
      const result = await api('POST', `/api/intake/${itemId}/approve`);
      toast(`Dodano ${result.approved.length} ${plural(result.approved.length, 'część', 'części', 'części')} ✓`,
        {action: {label: 'Rozłóż do pudełek', fn: () => { Object.assign(state, {tab: 'lists', listId: 'nobox'}); render(); }}});
    } catch (err) { toast(`Nie zapisano wszystkich: ${err.message}`, {bad: true}); }
    ids.forEach((id) => add.saving.delete(id));
    await loadAdd();
    loadSnapshotQuiet();
  },
  'prop-reject': (raw) => { add.rejecting = Number(raw); redrawAdd(); },
  'prop-reject-no': () => { add.rejecting = null; redrawAdd(); },
  'prop-reject-yes': async (raw) => {
    const form = new FormData();
    try {
      const r = await fetch(`/api/proposals/${Number(raw)}/reject`, {method: 'POST', body: form});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      toast('Odrzucono propozycję');
    } catch (err) { toast(`Nie odrzucono: ${err.message}`, {bad: true}); }
    add.rejecting = null;
    loadAdd();
  },
  'prop-fix': (raw) => {
    const p = add.proposals.find((x) => x.id === Number(raw));
    if (!p) return;
    const q = propQty(p.payload);
    openSheet({kind: 'fix', pid: p.id, draft: {name: p.payload.name, k: q.k, n: q.n ?? (q.k === 'approx' ? 50 : 1), category: p.payload.category || ''}});
  },
  'fx-kind': (raw) => { readFix(); state.sheet.draft.k = raw; renderSheet(); },
  'fx-dec': () => { readFix(); const d = state.sheet.draft; d.n = Math.max(0, d.n - (d.k === 'approx' ? 10 : 1)); renderSheet(); },
  'fx-inc': () => { readFix(); const d = state.sheet.draft; d.n += d.k === 'approx' ? 10 : 1; renderSheet(); },
  'fx-cat': () => { readFix(); state.sheet = {...state.sheet, kind: 'fixcat', query: ''}; renderSheet(); },
  'fx-pick-cat': (raw) => { state.sheet.draft.category = raw; state.sheet.kind = 'fix'; renderSheet(); },
  'fx-save': () => saveFix(false),
  'fx-save-approve': () => saveFix(true),
  'queued-toggle': () => { add.queuedOpen = !add.queuedOpen; redrawAdd(); },
  'done-toggle': () => { add.doneOpen = !add.doneOpen; redrawAdd(); },
  history: () => { add.history = true; add.doneOpen = true; redrawAdd(); },
  'copy-link': async () => {
    const p = state.byId.get(state.partId);
    // Numer, nazwa i link — wklejone do rozmowy z agentem wystarczą, żeby
    // wiedział, o którą część chodzi, bez zgadywania po opisie.
    const text = `#${p.id} ${p.name} — ${partLink(p.id)}`;
    try {
      await navigator.clipboard.writeText(text);
      toast(`Skopiowano: #${p.id}`);
    } catch {
      prompt('Skopiuj ręcznie:', text);
    }
  },
  'share-link': async () => {
    const p = state.byId.get(state.partId);
    try { await navigator.share({title: p.name, text: `#${p.id} ${p.name}`, url: partLink(p.id)}); } catch { /* anulowane */ }
  },
  'done-part': async (raw) => {
    try { applyCard(await api('GET', `/api/warsztat/parts/${Number(raw)}`)); openPart(Number(raw)); } catch (err) { toast(err.message, {bad: true}); }
  },
  'cam-cancel': () => closeCamera(),
  'cam-zoom': (raw) => setZoom(Number(raw)),
  'cam-shutter': () => shutter(),
});

function readFix() {
  const el = $('fx-name');
  if (el) state.sheet.draft.name = el.value;
}

async function saveFix(andApprove) {
  readFix();
  const sh = state.sheet;
  const d = sh.draft;
  const changes = {name: d.name.trim(), category: d.category, amount: d.k === 'some' ? 0 : d.n, instock_unknown: d.k !== 'exact'};
  try {
    await api('PATCH', `/api/proposals/${sh.pid}`, changes);
  } catch (err) { toast(`Nie zapisano: ${err.message}`, {bad: true}); return; }
  closeSheet();
  await loadAdd();
  if (andApprove) approveProposal(sh.pid);
  else toast('Poprawione ✓');
}

/* Udostępnienie z innej aplikacji: service worker odłożył zdjęcia do
   poczekalni i przekierował tu z podsumowaniem w adresie. */
function handleShareParams() {
  const params = new URLSearchParams(location.search);
  const shared = Number(params.get('udostepniono') || 0);
  const text = params.get('tekst');
  const error = params.get('blad');
  if (!shared && !text && !error) return;
  state.tab = 'add';
  if (shared) toast(shared === 1 ? 'Udostępnione zdjęcie w kolejce' : `${shared} udostępnionych zdjęć w kolejce`);
  if (text) openSheet({kind: 'addtext', text});
  if (error) toast(`Udostępnianie: ${error}`, {bad: true});
  history.replaceState(null, '', '/');
}

document.addEventListener('keydown', (event) => {
  if (!DESK.matches || state.tab !== 'add' || state.sheet || ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
  const index = add.proposals.findIndex((p) => p.id === add.selProp);
  if (event.key === 'Enter' && add.selProp) { event.preventDefault(); approveProposal(add.selProp); }
  if (event.key === 'ArrowDown' && index < add.proposals.length - 1) { add.selProp = add.proposals[index + 1].id; redrawAdd(); }
  if (event.key === 'ArrowUp' && index > 0) { add.selProp = add.proposals[index - 1].id; redrawAdd(); }
});

document.addEventListener('input', (event) => {
  if (event.target.id === 'add-text' && state.sheet && state.sheet.kind === 'addtext') {
    state.sheet.text = event.target.value;
    const pos = event.target.selectionStart;
    renderSheet();
    const el = $('add-text');
    el.focus(); el.setSelectionRange(pos, pos);
  }
});

/* --- rysowanie ------------------------------------------------------- */

function renderView() {
  const view = $('view');
  const keepScroll = view.querySelector('.scroll');
  const top = keepScroll ? keepScroll.scrollTop : 0;
  if (state.tab === 'search') view.innerHTML = searchView();
  else if (state.tab === 'browse') view.innerHTML = browseView();
  else if (state.tab === 'add') view.innerHTML = addView();
  else view.innerHTML = listsView();
  const scroll = view.querySelector('.scroll');
  if (scroll && state.keepScroll) scroll.scrollTop = top;
  state.keepScroll = false;
  const q = $('q');
  if (q) q.oninput = () => {
    state.query = q.value;
    state.shown = RESULTS_STEP;
    $('results').innerHTML = resultsHtml();
    view.querySelector('.clear').hidden = !q.value;
  };
}

function render() {
  $('offline').hidden = !state.offline;
  renderTabs();
  renderView();
  renderCard();
  renderAddRight();
}

function focusSearch() {
  const q = $('q');
  if (q) try { q.focus({preventScroll: true}); } catch { /* starsze przeglądarki */ }
}

/* --- zdarzenia ------------------------------------------------------- */

document.addEventListener('click', (event) => {
  const el = event.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  const raw = el.dataset.id;
  const id = raw === '' || raw == null ? null : (Number.isNaN(Number(raw)) ? raw : Number(raw));
  const phone = !DESK.matches;
  switch (act) {
    case 'tab':
      if (state.tab === id) {
        if (id === 'search') state.query = '';
        if (id === 'browse') { state.catId = null; state.boxId = null; }
        if (id === 'lists') state.listId = null;
      }
      state.tab = id;
      state.shown = RESULTS_STEP;
      if (phone) { state.partId = null; history.replaceState(null, '', '/'); }
      render();
      if (id === 'search') focusSearch();
      if (id === 'add') loadAdd();
      break;
    case 'part': state.keepScroll = true; openPart(id); break;
    case 'close': event.stopPropagation(); state.partId = null; history.replaceState(null, '', '/'); renderCard(); break;
    case 'clear': resetFilters(); state.query = ''; renderView(); focusSearch(); break;
    case 'more': state.shown += RESULTS_STEP; state.keepScroll = true; renderView(); break;
    case 'list': state.tab = 'lists'; state.listId = id; state.shown = RESULTS_STEP; if (phone) state.partId = null; render(); break;
    case 'fm': case 'fpk': case 'fsort':
      // Surowy tekst, nie `id`: „0805” zamienione na liczbę to 805.
      if (act === 'fm') { state.flt.m = raw || null; state.flt.pk = null; }
      if (act === 'fpk') state.flt.pk = raw || null;
      if (act === 'fsort') state.sort = raw;
      state.shown = RESULTS_STEP;
      // Na Szukaj odświeżamy tylko wyniki, żeby pole nie straciło fokusu.
      if (state.tab === 'search' && $('results')) $('results').innerHTML = resultsHtml();
      else { state.keepScroll = true; renderView(); }
      break;
    case 'catview': state.catView = id; localStorage.setItem(CATVIEW_KEY, id); renderView(); break;
    case 'mode': state.browseMode = id; state.shown = RESULTS_STEP; renderView(); break;
    case 'cat':
      state.tab = 'browse'; state.browseMode = 'cat'; state.catId = id; state.shown = RESULTS_STEP;
      resetFilters();
      if (phone) state.partId = null;
      render();
      break;
    case 'box':
      state.tab = 'browse'; state.browseMode = 'box'; state.boxId = id; state.shown = RESULTS_STEP;
      if (phone) state.partId = null;
      render();
      break;
    case 'tech': state.techOpen = !state.techOpen; renderCard(); break;
    case 'viewer-close': closeViewer(); break;
    case 'prev': case 'next': {
      const card = state.cards.get(state.partId) || {};
      const p = state.byId.get(state.partId);
      const photos = card.photos && card.photos.length ? card.photos.map((x) => x.url) : [bigPhoto(p.thumb)];
      state.photoIdx = (state.photoIdx + (act === 'next' ? 1 : -1) + photos.length) % photos.length;
      renderCard();
      break;
    }
    default:
      if (CARD_ACTIONS[act]) CARD_ACTIONS[act](raw ?? '');
      break;
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (state.sheet) closeSheet();
  else if (state.viewer) closeViewer();
  else if (state.partId != null && !DESK.matches) { state.partId = null; renderCard(); }
});

window.addEventListener('online', () => { state.offline = false; loadSnapshot(); flushOutbox(); });
window.addEventListener('offline', () => { state.offline = true; render(); });
DESK.addEventListener('change', render);
// Powrót do aplikacji po godzinie w kieszeni ma pokazać aktualny stan.
document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadSnapshot(); loadAdd(); } });

applyTheme();
/* Zakładka z adresu: pasek na stronie /dodaj prowadzi tu linkami
   `/#przegladaj` i `/#listy`, bo to osobna strona. */
const HASH_TABS = {'#szukaj': 'search', '#przegladaj': 'browse', '#listy': 'lists', '#dodaj': 'add'};
function tabFromHash() {
  const part = location.hash.match(/^#c\/(\d+)$/);
  if (part) {
    // Bez zrzutu nie ma gdzie wstawić karty — otwieramy po jego pobraniu.
    if (state.snap) openFromLink(Number(part[1])); else state.pendingLink = Number(part[1]);
    return false;
  }
  if (!HASH_TABS[location.hash]) return false;
  state.tab = HASH_TABS[location.hash];
  history.replaceState(null, '', '/');
  return true;
}
/* Link do części (`/#c/591`): otwiera kartę, nawet gdy części nie ma
   jeszcze w zrzucie z pamięci telefonu (np. świeżo zatwierdzona). */
async function openFromLink(id) {
  if (!state.byId.has(id)) {
    try { applyCard(await api('GET', `/api/warsztat/parts/${id}`)); } catch { toast(`Nie ma części #${id}`, {bad: true}); history.replaceState(null, '', '/'); return; }
  }
  openPart(id);
}
tabFromHash();
// Ten sam dokument, inny `#` (np. link wklejony w otwartą kartę).
window.addEventListener('hashchange', () => { if (tabFromHash()) render(); });
window.WARSZTAT_VERSION = APP_VERSION;
handleShareParams();
render();
if (state.tab === 'search') focusSearch();
loadSnapshot().then(() => {
  if (state.pendingLink && state.snap) openFromLink(state.pendingLink);
  state.pendingLink = null;
});
loadAdd().then(flushOutbox);
// Ten sam service worker co dla „Dodaj”: Warsztat jest teraz stroną
// startową zainstalowanej aplikacji, więc stąd ma się zarejestrować.
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
