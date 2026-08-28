const CACHE="sagasmith-shell-v11";
const SHELL=[
  "/",
  "/styles.css",
  "/app.js",
  "/assets/api/client.js",
  "/assets/auth/controller.js",
  "/assets/campaign/controller.js",
  "/assets/components/dom.js",
  "/assets/components/pwa.js",
  "/assets/components/toast.js",
  "/assets/forge/catalog.js",
  "/assets/forge/controller.js",
  "/assets/forge/moderation.js",
  "/assets/forge/shared.js",
  "/assets/forge/studio.js",
  "/assets/identity/controller.js",
  "/assets/module-studio/controller.js",
  "/assets/room/characters.js",
  "/assets/room/combat-grid.js",
  "/assets/room/combat-grid-state.js",
  "/assets/room/encounter-planner-state.js",
  "/assets/room/controller.js",
  "/assets/room/model.js",
  "/assets/room/timeline.js",
  "/assets/room/view.js",
  "/assets/state/store.js",
  "/manifest.webmanifest",
  "/sagasmith-icon.svg",
  "/sagasmith-grid-texture.webp",
];
self.addEventListener("install",event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL))));
self.addEventListener("activate",event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key))))));
self.addEventListener("fetch",event=>{const url=new URL(event.request.url);if(event.request.method!=="GET"||url.origin!==self.location.origin||url.pathname.startsWith("/api/"))return;event.respondWith(fetch(event.request).then(response=>{if(response.ok){const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy))}return response}).catch(()=>caches.match(event.request).then(cached=>cached||(event.request.mode==="navigate"?caches.match("/"):Response.error()))))});
