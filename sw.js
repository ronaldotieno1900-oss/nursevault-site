/* NurseVault offline shell
   Opens the site quickly and keeps it working when the network is weak.
   Payments, login and downloads still need internet.
   Change the version number below whenever you want every visitor to refresh their saved copy. */
const CACHE = "nursevault-shell-v3";
const SHELL = [
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
  "./samples/sample-1.jpg",
  "./samples/sample-2.jpg",
  "./samples/sample-3.jpg",
  "./samples/sample-4.jpg",
  "./samples/sample-5.jpg"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.all(SHELL.map((url) => cache.add(url).catch(() => { /* skip a missing file */ })))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  let url;
  try { url = new URL(req.url); } catch (e) { return; }

  // Only handle this site's own files. Everything else (your account and payment
  // workers, Paystack, Google sign-in, fonts) goes straight to the network.
  if (url.origin !== self.location.origin) return;

  // Page loads: always try the network first so updates show up, and fall back to
  // the saved copy when offline.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put("./index.html", copy));
          }
          return res;
        })
        .catch(() => caches.match("./index.html"))
    );
    return;
  }

  // Images and other files: show the saved copy right away, and refresh it in the
  // background so the next visit has the latest version.
  event.respondWith(
    caches.match(req).then((hit) => {
      const fresh = fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => hit);
      return hit || fresh;
    })
  );
});
