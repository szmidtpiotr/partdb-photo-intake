/* Service worker: tylko powłoka aplikacji, nigdy dane.
 *
 * Cache'owanie odpowiedzi z /api/ dałoby najgorszy możliwy efekt w tym
 * systemie — telefon pokazywałby nieaktualną kolejkę, a użytkownik
 * fotografowałby drugi raz to, co już wysłał. Dane zawsze z sieci.
 */

// Numer w nazwie podbijamy przy KAŻDEJ zmianie powłoki. Stare pule są
// kasowane przy aktywacji, więc telefon nie zostaje ze starym kodem —
// a właśnie to raz się zdarzyło: serwer miał nową wersję, a zainstalowana
// aplikacja nadal wykonywała poprzednią.
// Poczekalnia jest wspólna z aplikacją: to ten sam magazyn IndexedDB,
// więc zdjęcie udostępnione z Lensa i zdjęcie nieudane z aparatu czekają
// obok siebie i wychodzą tą samą drogą.
importScripts('/outbox.js?v=25');

const CACHE = 'bom-intake-v25';
const SHELL = [
  '/',
  '/index.html',
  '/warsztat/warsztat.css?v=25',
  '/warsztat/warsztat.js?v=25',
  '/app.js?v=25',
  '/outbox.js?v=25',
  '/styles.css?v=25',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* Udostępnianie z innych aplikacji (#65).
 *
 * Android wysyła zdjęcie POST-em na `/share` — adres, pod którym po stronie
 * serwera nic nie ma i nie musi być, bo żądanie nigdy nie wychodzi z
 * telefonu. Zdjęcie ląduje w tej samej poczekalni co nieudane wysyłki
 * (#60), więc udostępnienie działa też bez zasięgu: aplikacja wyśle je
 * przy najbliższej okazji.
 *
 * Odpowiedzią jest przekierowanie na stronę główną. Bez niego użytkownik
 * zostałby na pustym `/share` i zobaczyłby, że „nie działa".
 */
async function receiveShare(request) {
  const target = new URL('/', self.location.origin);
  try {
    const form = await request.formData();
    const photos = form.getAll('photos').filter((file) => file && file.size);

    for (const photo of photos) {
      await Outbox.add({
        blob: photo,
        name: photo.name,
        note: 'udostępnione z innej aplikacji',
      });
    }
    target.searchParams.set('udostepniono', String(photos.length));

    // Z Lensa czy ze sklepu przychodzi czasem sam tekst albo adres.
    // Nie zgadujemy, co z nim zrobić — ląduje w polu dyktowania, żeby
    // użytkownik go uzupełnił i wysłał świadomie.
    const text = [form.get('title'), form.get('text'), form.get('url')]
      .filter(Boolean)
      .join(' ')
      .trim();
    if (!photos.length && text) target.searchParams.set('tekst', text);
    if (!photos.length && !text) target.searchParams.set('blad', 'puste udostępnienie');
  } catch (err) {
    target.searchParams.set('blad', String((err && err.message) || err));
  }
  // 303: przeglądarka ma pobrać stronę główną GET-em, a nie powtórzyć POST.
  return Response.redirect(target.toString(), 303);
}

self.addEventListener('fetch', (event) => {
  const { request } = event;

  if (request.method === 'POST'
      && new URL(request.url).pathname === '/share') {
    event.respondWith(receiveShare(request));
    return;
  }

  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/health') return;

  // Powłoka: najpierw sieć (żeby poprawka doszła bez czyszczenia cache),
  // przy braku zasięgu wersja z cache — aplikacja otwiera się w piwnicy.
  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match('/index.html')))
  );
});
