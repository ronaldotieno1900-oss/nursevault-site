/* NurseVault offline shell (v4)
   Opens the site quickly and keeps it working when the network is weak.
   Payments, login, downloads and AI still need internet.
   Change CACHE below whenever you want every visitor to refresh their saved copy. */
const CACHE = "nursevault-shell-v4";
const SHELL = [
  "./",
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
const NAV_TIMEOUT_MS = 4000; // on slow mobile data, show the saved copy instead of a blank wait

// Hosts such as Cloudflare Pages redirect /index.html to /. Browsers refuse to show a saved
// response that came from a redirect, so we re-wrap it as a plain response before saving.
async function clean(res) {
  if (!res || !res.redirected) return res;
  const body = await res.blob();
  return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
}

function save(cache, key, res) {
  // Only whole, successful, same-origin responses. (Partial 206 responses make cache.put throw.)
  if (res && res.status === 200 && res.type === "basic") return cache.put(key, res);
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.all(SHELL.map((url) =>
        fetch(url).then(clean).then((res) => save(cache, url, res)).catch(() => { /* skip a missing file */ })
      ))
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

function offlinePage() {
  return caches.match("./index.html").then((r) => r || caches.match("./"));
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  if (req.headers.has("range")) return; // video/audio/partial downloads go straight to the network

  let url;
  try { url = new URL(req.url); } catch (e) { return; }

  // Only this site's own files. Your account/payment workers, Paystack, Google sign-in and
  // fonts are other origins and always go straight to the network (never saved).
  if (url.origin !== self.location.origin) return;

  // Page loads: network first so updates show up; if it is slow (4s) or offline, use the saved copy.
  if (req.mode === "navigate") {
    event.respondWith((async () => {
      const network = fetch(req).then(async (res) => {
        const copy = await clean(res.clone());
        if (res.ok) caches.open(CACHE).then((c) => { save(c, "./index.html", copy); save(c, "./", copy.clone ? copy.clone() : copy); }).catch(() => {});
        return res;
      });
      const timeout = new Promise((resolve) => setTimeout(() => resolve(null), NAV_TIMEOUT_MS));
      try {
        const first = await Promise.race([network, timeout]);
        if (first) return first;
        const cached = await offlinePage();
        if (cached) { network.catch(() => {}); return cached; } // keep downloading in the background
        return await network;
      } catch (e) {
        return (await offlinePage()) || Response.error();
      }
    })());
    return;
  }

  // Images and other files: show the saved copy right away, refresh it in the background.
  event.respondWith(
    caches.match(req).then((hit) => {
      const fresh = fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === "basic") {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => hit);
      return hit || fresh;
    })
  );
});
