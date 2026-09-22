"use client";

/** Turn on the morning digest.
 *
 * WHAT WAS ACTUALLY MISSING
 * The server half has been built and SCHEDULED since Phase 8: `hourly_digest`
 * runs every hour and sends only to tenants for whom it is currently 07:00
 * where they are, deduped by a unique index on (user_id, kind, sent_on). The
 * service-account credential was in place too. The one missing piece was a
 * device token — nothing in the product had ever called `POST /push/devices`,
 * so the scheduler swept an empty list every hour, correctly, forever.
 *
 * THE TIMEZONE COMES FROM THE DEVICE, NOT THE ACCOUNT
 * "07:00 local" is a property of where the phone is. `Intl` gives the
 * browser's IANA zone, which is the honest answer for THIS device, and a user
 * who travels then gets their digest at 07:00 where they actually are.
 *
 * PERMISSION IS REQUESTED ON A CLICK, NEVER ON LOAD
 * A permission prompt fired at page load is the pattern browsers now
 * penalise and users reflexively deny — and a denial is sticky, so asking
 * badly once costs the feature permanently.
 */

import { useCallback, useEffect, useState } from "react";
import { disablePushDevices, listPushDevices, registerPushDevice } from "@/lib/api";

const CONFIG = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY ?? "",
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID ?? "",
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_SENDER_ID ?? "",
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID ?? "",
};
const VAPID_KEY = process.env.NEXT_PUBLIC_FIREBASE_VAPID_KEY ?? "";

export default function PushNotifications() {
  const [count, setCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listPushDevices()
      .then((r) => setCount(r.devices.filter((d) => d.enabled !== false).length))
      .catch(() => setCount(0));
  }, []);
  useEffect(refresh, [refresh]);

  const configured = Boolean(CONFIG.apiKey && CONFIG.appId && VAPID_KEY);
  const supported =
    typeof window !== "undefined" && "Notification" in window && "serviceWorker" in navigator;

  async function enable() {
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        // Sticky in every browser: there is no second prompt, so say what to
        // do instead of implying a retry will work.
        // "Padlock" was wrong and this is the screen where being wrong costs
        // most: the denial is sticky, so the user gets one shot at following
        // the instructions. There IS no padlock on http://localhost — Chrome
        // shows a sliders/tune icon or an ⓘ there, and a padlock only on
        // HTTPS. Naming both, plus the settings URL as a fallback, because
        // the icon differs by browser and version and the settings page does
        // not.
        setErr(
          permission === "denied"
            ? "Notifications are blocked for this site. Click the icon just left of the " +
              "address bar (a sliders or ⓘ icon on localhost, a padlock on https), set " +
              "Notifications to Allow, reload, then try again. Or clear it at " +
              "chrome://settings/content/notifications."
            : "Permission was dismissed — click the button again when you're ready.",
        );
        return;
      }

      // Imported here rather than at module scope so the ~200KB messaging
      // bundle is fetched only by someone who actually opted in.
      const { initializeApp } = await import("firebase/app");
      const { getMessaging, getToken, isSupported } = await import("firebase/messaging");
      if (!(await isSupported())) {
        setErr("This browser cannot receive web push.");
        return;
      }

      // REGISTER, THEN WAIT FOR IT TO BE ACTIVE.
      //
      // `register()` resolves as soon as the REGISTRATION object exists — the
      // worker itself is still `installing` at that moment. `PushManager
      // .subscribe` needs an ACTIVE one, so handing the fresh registration
      // straight to `getToken` fails with "Subscription failed - no active
      // Service Worker" on the first attempt and then works on the second,
      // once the install has finished in the background. A bug that fixes
      // itself on retry is the worst kind to leave in: it looks like flakiness
      // rather than a race.
      await navigator.serviceWorker.register("/firebase-messaging-sw.js");
      // `ready` resolves only when a registration in this scope has an active
      // worker, which is exactly the condition subscribe requires.
      const registration = await navigator.serviceWorker.ready;

      const token = await getToken(getMessaging(initializeApp(CONFIG)), {
        vapidKey: VAPID_KEY,
        serviceWorkerRegistration: registration,
      });
      if (!token) {
        setErr("Firebase did not return a device token.");
        return;
      }

      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata";
      await registerPushDevice(token, timezone);
      setNote(`This device is registered. You'll get a digest at 07:00 ${timezone}.`);
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    setErr(null);
    try {
      await disablePushDevices();
      setNote("Turned off for every device on this account.");
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 18 }}>
      <h2 className="ui-h3">Morning digest</h2>

      {!configured ? (
        <p className="ui-sub">
          Push isn&apos;t configured on this deployment — the Firebase web keys are missing from{" "}
          <code>.env</code>. Nothing is broken; there is simply nothing to register with.
        </p>
      ) : !supported ? (
        <p className="ui-sub">
          This browser can&apos;t receive web push. Safari needs the app added to the Home Screen
          first.
        </p>
      ) : (
        <>
          <p className="ui-sub" style={{ marginBottom: 10 }}>
            One notification at <b style={{ color: "var(--ink)" }}>07:00 your time</b> with what
            to wear for the day — built from your calendar when it&apos;s connected, and from your
            wardrobe either way. Not a marketing channel: it is sent once a day and never more.
          </p>
          {count ? (
            <>
              <p className="ui-sub" style={{ marginBottom: 10 }}>
                {count} device{count > 1 ? "s" : ""} registered.
              </p>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button className="ui-btn" onClick={() => void enable()} disabled={busy}>
                  {busy ? "…" : "Add this device"}
                </button>
                <button className="ui-btn" onClick={() => void disable()} disabled={busy}>
                  Turn off everywhere
                </button>
              </div>
            </>
          ) : (
            <button className="ui-btn primary" onClick={() => void enable()} disabled={busy}>
              {busy ? "…" : "Turn on the morning digest"}
            </button>
          )}
        </>
      )}

      {note ? <p className="ui-sub" style={{ color: "var(--ok)", marginTop: 10 }}>{note}</p> : null}
      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}
    </div>
  );
}
