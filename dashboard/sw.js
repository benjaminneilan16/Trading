// Minimal service worker – behövs mest för att iOS Safari ska
// tillåta "Lägg till på hemskärmen" med app-liknande beteende.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // Ingen offline-cache i denna första version — bara pass-through.
  event.respondWith(fetch(event.request));
});
