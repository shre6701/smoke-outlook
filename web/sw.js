// Offline support: the app shell and libraries are cached; data is fetched fresh when
// possible and falls back to the last copy (the page shows how old the data is).
const SHELL = "shell-v2";
const DATA = "data-v1";
const FILES = [
  "./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icon-192.png",
  "https://unpkg.com/maplibre-gl@5.24.1/dist/maplibre-gl.css",
  "https://unpkg.com/maplibre-gl@5.24.1/dist/maplibre-gl.js",
  "https://unpkg.com/deck.gl@9.4.1/dist.min.js",
];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => ![SHELL, DATA].includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  const isData = url.pathname.startsWith("/api/") || url.pathname.includes("/font/");
  if (isData) {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          if (r.ok) caches.open(DATA).then((c) => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match(e.request)),
    );
  } else {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          if (r.ok) caches.open(SHELL).then((c) => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match(e.request)),
    );
  }
});
