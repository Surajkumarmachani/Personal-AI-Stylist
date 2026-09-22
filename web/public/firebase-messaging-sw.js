/* GENERATED from the repo-root .env. Firebase messaging service worker.
 *
 * WHY THIS FILE IS SEPARATE AND NOT BUNDLED
 * A service worker has to be served from the origin root at a stable path —
 * the browser fetches `/firebase-messaging-sw.js` itself, outside the app's
 * bundle, and it runs when the page is CLOSED. That is the whole point: a
 * 7am digest has to arrive when nobody is looking at the tab.
 *
 * WHY THE CONFIG IS INLINED RATHER THAN IMPORTED
 * A service worker cannot read `process.env` and has no access to the app's
 * module graph, so the values are written here at build time by
 * scripts/gen_firebase_sw.py. They are public by design — they ship to every
 * browser that loads the site — which is exactly why the SENDING credential
 * is a different thing living in secrets/ and never here.
 */
importScripts("https://www.gstatic.com/firebasejs/10.14.1/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.14.1/firebase-messaging-compat.js");

firebase.initializeApp({
  apiKey: "AIzaSyCtMU_dxFrO2qPifBP1R2Xxai0NneVoeTE",
  projectId: "personal-ai-stylist-a07c0",
  messagingSenderId: "176194440049",
  appId: "1:176194440049:web:d873e309366aa98b2684d0",
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  const n = payload.notification || {};
  self.registration.showNotification(n.title || "Your stylist", {
    body: n.body || "",
    icon: "/icon-192.png",
    // Collapses repeats: a digest resent after a retry must not stack three
    // identical banners on the lock screen.
    tag: (payload.data && payload.data.kind) || "digest",
    data: payload.data || {},
  });
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Focus an open tab if there is one rather than opening a second copy of
  // the app, which is what a plain openWindow() does.
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((tabs) => {
      for (const tab of tabs) {
        if ("focus" in tab) return tab.focus();
      }
      return clients.openWindow("/");
    }),
  );
});
