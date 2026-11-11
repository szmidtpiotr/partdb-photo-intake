/* Poczekalnia zdjęć, które nie poszły (#60).
 *
 * Sedno: w warsztacie zasięg bywa żaden, a zdjęcie zrobione i pokazane
 * w kolejce użytkownik ma prawo uważać za bezpieczne. Trzymanie go
 * w pamięci karty łamie tę obietnicę po cichu — zamknięcie karty gubi
 * plik i nikt się o tym nie dowiaduje. IndexedDB przeżywa zamknięcie
 * aplikacji, restart telefonu i tydzień w kieszeni.
 *
 * Plik jest klasycznym skryptem, nie modułem, bo korzysta z niego także
 * service worker (`importScripts`) — przy udostępnianiu z Lensa (#65)
 * to on odbiera zdjęcia, zanim aplikacja się w ogóle otworzy.
 */

const Outbox = (() => {
  const DB_NAME = 'bom-outbox';
  const STORE = 'photos';
  const DB_VERSION = 1;

  let opening = null;

  function open() {
    if (!opening) {
      opening = new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(STORE)) {
            db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    return opening;
  }

  async function run(mode, work) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, mode);
      const request = work(transaction.objectStore(STORE));
      transaction.onabort = () => reject(transaction.error);
      transaction.onerror = () => reject(transaction.error);
      transaction.oncomplete = () => resolve(request ? request.result : undefined);
    });
  }

  /* `blob` zamiast `File`: strukturalne klonowanie do IndexedDB obsługuje
     oba, ale po odczycie i tak potrzebna jest osobna nazwa pliku, więc
     trzymamy ją jawnie zamiast liczyć na to, co przetrwa serializację. */
  function add(entry) {
    const record = {
      blob: entry.blob,
      name: entry.name || 'zdjecie.jpg',
      locationHint: entry.locationHint || '',
      parentId: entry.parentId || null,
      note: entry.note || '',
      createdAt: entry.createdAt || new Date().toISOString(),
    };
    return run('readwrite', (store) => store.add(record));
  }

  function all() {
    return run('readonly', (store) => store.getAll());
  }

  function remove(id) {
    return run('readwrite', (store) => store.delete(id));
  }

  function update(id, patch) {
    return run('readwrite', (store) => {
      const request = store.get(id);
      request.onsuccess = () => {
        const record = request.result;
        if (record) store.put({ ...record, ...patch });
      };
      return request;
    });
  }

  async function count() {
    return (await all()).length;
  }

  return { add, all, remove, update, count };
})();

// Service worker widzi `Outbox` przez importScripts, okno przez <script>.
if (typeof self !== 'undefined') self.Outbox = Outbox;
