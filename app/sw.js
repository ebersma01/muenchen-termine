// Offline-Cache: App-Dateien aus dem Cache, Termine immer zuerst aus dem Netz.
const CACHE = "termine-v1";
const SHELL = ["./", "index.html", "styles.css", "app.js", "manifest.webmanifest", "icon.svg",
  "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.includes("/data/")) {
    e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request, { ignoreSearch: true })));
  } else {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  }
});
