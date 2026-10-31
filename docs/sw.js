/* Service worker de «CF Pozuelo Liga».
 *
 * Qué guarda y cómo lo sirve:
 *   - La app (index.html, manifest, iconos):  primero la copia guardada y, en segundo plano, la actualiza
 *     (carga instantánea; los cambios del código se ven a la segunda apertura).
 *   - Los datos (datos_liga.js / .json):       primero la red; si no hay red o tarda más de 3 s, la última copia.
 *   - Tailwind y la tipografía (externos):     igual que la app; se guardan al instalar y se renuevan al usarlos.
 *   - /api/live-updates (tiempo real) y cualquier otra cosa: NO se tocan, van directas a la red.
 *
 * Requiere https o localhost. Si cambias el contenido de SHELL o EXTERNOS, sube VERSION.
 */
const VERSION = 'v19';
const CACHE_APP = 'liga-app-' + VERSION;
const CACHE_DATOS = 'liga-datos';                 // no depende de VERSION: conserva los últimos datos entre versiones
const CACHE_EXTERNO = 'liga-externo-' + VERSION;
const CACHE_ESCUDOS = 'liga-escudos';             // escudos de los equipos: se van guardando según se ven

const INDEX = new URL('index.html', self.registration.scope).href;
const CRITICOS = ['index.html', 'manifest.json'];                       // si faltan, no se instala
const SHELL = [...CRITICOS, 'logo.png', 'apple-touch-icon.png', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png'];
const DATOS = ['datos_liga.js', 'datos_liga.json'];
const EXTERNOS = [                                                       // los mismos que usa index.html
  'https://cdn.tailwindcss.com',
  'https://fonts.googleapis.com/css2?family=Montserrat:wght@300;500;600;800&display=swap',
];
const NOMBRES_SHELL = new Set(SHELL);
const NOMBRES_DATOS = new Set(DATOS);
const absoluta = (ruta) => new URL(ruta, self.registration.scope).href;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const app = await caches.open(CACHE_APP);
    await app.addAll(CRITICOS.map((r) => new Request(absoluta(r), { cache: 'reload' })));
    // El resto es "lo que haya": un fallo aquí no impide instalar.
    await Promise.allSettled(SHELL.filter((r) => !CRITICOS.includes(r)).map((r) => app.add(new Request(absoluta(r), { cache: 'reload' }))));
    const datos = await caches.open(CACHE_DATOS);
    await Promise.allSettled(DATOS.map((r) => datos.add(new Request(absoluta(r), { cache: 'reload' }))));
    const externo = await caches.open(CACHE_EXTERNO);
    await Promise.allSettled(EXTERNOS.map(async (u) => externo.put(u, await fetch(new Request(u, { mode: 'no-cors' })))));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const vigentes = new Set([CACHE_APP, CACHE_DATOS, CACHE_EXTERNO, CACHE_ESCUDOS]);
    for (const nombre of await caches.keys()) {
      if (nombre.startsWith('liga-') && !vigentes.has(nombre)) await caches.delete(nombre);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if ((req.headers.get('accept') || '').includes('text/event-stream')) return;      // tiempo real

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) {                                         // Tailwind, tipografías…
    if (['script', 'style', 'font', 'image'].includes(req.destination)) {
      event.respondWith(cacheLuegoRed(event, req, CACHE_EXTERNO, req.url, { ignoreVary: true }));
    }
    return;
  }
  if (url.pathname.includes('/api/')) return;

  if (/\/escudos\/[^/]+$/.test(url.pathname)) {                                  // escudos de equipos
    event.respondWith(cacheLuegoRed(event, req, CACHE_ESCUDOS, url.origin + url.pathname));
    return;
  }
  const nombre = url.pathname.split('/').pop();
  const clave = url.origin + url.pathname;                                           // sin ?parámetros
  if (req.mode === 'navigate') event.respondWith(navegacion(event));
  else if (NOMBRES_DATOS.has(nombre)) event.respondWith(redPrimero(event, req, CACHE_DATOS, clave));
  else if (NOMBRES_SHELL.has(nombre)) event.respondWith(cacheLuegoRed(event, req, CACHE_APP, clave));
});

/* Copia guardada al instante y actualización en segundo plano. */
async function cacheLuegoRed(event, req, nombreCache, clave, opciones = {}) {
  const cache = await caches.open(nombreCache);
  const guardado = await cache.match(clave, opciones);
  const red = fetch(req).then((r) => { if (r.ok || r.type === 'opaque') cache.put(clave, r.clone()); return r; });
  if (guardado) { event.waitUntil(red.catch(() => {})); return guardado; }
  return red;
}

/* La red manda; sin red (o si tarda más de 3 s) se usa la última copia. */
async function redPrimero(event, req, nombreCache, clave) {
  const cache = await caches.open(nombreCache);
  const guardado = await cache.match(clave);
  const red = fetch(req).then((r) => { if (r.ok) cache.put(clave, r.clone()); return r; });
  if (!guardado) return red;
  event.waitUntil(red.catch(() => {}));
  return Promise.race([
    red.then((r) => (r.ok ? r : guardado)).catch(() => guardado),
    new Promise((resolver) => setTimeout(() => resolver(guardado), 3000)),
  ]);
}

/* Abrir la app: siempre index.html (es una sola página), desde la copia guardada si la hay. */
async function navegacion(event) {
  const cache = await caches.open(CACHE_APP);
  const guardado = await cache.match(INDEX);
  const red = fetch(new Request(INDEX, { cache: 'no-cache' })).then((r) => { if (r.ok) cache.put(INDEX, r.clone()); return r; });
  if (guardado) { event.waitUntil(red.catch(() => {})); return guardado; }
  try {
    return await red;
  } catch (e) {
    return new Response('Sin conexión y sin copia guardada. Abre la app una vez con conexión.',
      { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  }
}
