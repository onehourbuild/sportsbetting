/* Edge Finder service worker.
   - /static/*: stale-while-revalidate (serve the cached copy, refetch in the background)
     with a cache name derived from the app version, so a deploy that changes app.css or
     app.js reaches the phone on the next load instead of never.
   - everything else (authenticated pages, htmx partials): network-only. Pages are never
     written to the cache, so a shared or lost phone can not replay ledger/settings HTML
     from the cache; offline, a small inline page is served instead.
   __APP_VERSION__ is substituted by the /sw.js route from app.APP_VERSION. */
const VERSION = "__APP_VERSION__";
const CACHE = "edges-v2-" + VERSION;
const PRECACHE = [
  "/static/app.css?v=" + VERSION,
  "/static/app.js?v=" + VERSION,
  "/static/htmx.min.js?v=" + VERSION,
  "/static/icons/icon-192.png",
];

const OFFLINE_HTML =
  '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<meta name="color-scheme" content="dark light"><title>Offline</title>' +
  '<style>body{font-family:system-ui,sans-serif;margin:0;padding:24px;background:#0b0f14;color:#e6edf3}' +
  'a{color:#7cc4ff}p{max-width:32rem}</style></head><body>' +
  '<h1>Offline</h1><p>Edge Finder needs a connection to show live prices and your ledger. ' +
  '<a href="/">Try again</a>.</p></body></html>';

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    /* Stale-while-revalidate: answer from the cache when possible, always refresh it. */
    const refresh = fetch(req).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy));
      }
      return res;
    });
    event.waitUntil(refresh.catch(() => undefined));
    event.respondWith(caches.match(req).then((hit) => hit || refresh));
    return;
  }

  /* Network-only for pages: no cache reads, no cache writes. */
  event.respondWith(
    fetch(req).catch(() => {
      if (req.headers.get("HX-Request") === "true" || req.mode !== "navigate") {
        return Response.error();
      }
      return new Response(OFFLINE_HTML, {
        status: 503,
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    })
  );
});
