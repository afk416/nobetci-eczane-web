/* Nobetcim service worker — uygulama kabuğu önbelleği + çevrimdışı yedek.
   Nöbet verisi (/api/) ASLA önbelleğe alınmaz; her zaman ağdan taze gelir. */
const CACHE = "nobetcim-v1";
const SHELL = ["/", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // API: önbelleğe alma, dokunma (varsayılan ağ) — nöbet verisi hep taze
  if (url.pathname.startsWith("/api/")) return;

  // Sayfa gezinmesi: ağ-öncelikli, çevrimdışıysa önbellekteki "/"
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/")));
    return;
  }

  // Diğer statikler (ikon, harita css/js): önbellek-öncelikli
  event.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req).then((resp) => {
        if (resp && resp.status === 200 && (url.origin === self.location.origin)) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      }).catch(() => cached)
    )
  );
});
