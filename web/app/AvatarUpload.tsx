"use client";

/** Set the profile picture.
 *
 * DIMENSIONS ARE CHECKED IN THE BROWSER, like AddGarments does, and for the
 * same reason: a 40px thumbnail renders as a blur in a 32px circle on a 2x
 * screen, and telling someone that after a round trip is latency spent to
 * deliver bad news.
 *
 * THE SERVER STILL RE-CHECKS THE KEY. This component sends the key it was
 * given, but `PUT /me/avatar` verifies the prefix and that the path is inside
 * the caller's own folder — a client is not a place to enforce whose photo is
 * whose.
 */

import { useEffect, useRef, useState } from "react";
import { clearAvatar, getAvatar, uploadAvatar } from "@/lib/api";

// A 32px circle at 2x is 64px; below this it is visibly soft.
const MIN_SIDE_PX = 128;

async function shortSide(file: File): Promise<number | null> {
  try {
    const bitmap = await createImageBitmap(file);
    const side = Math.min(bitmap.width, bitmap.height);
    bitmap.close();
    return side;
  } catch {
    // Unreadable or an unsupported format — let the server answer.
    return null;
  }
}

export default function AvatarUpload({ email }: { email: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Full-size view. The 32px circle in the top bar and even the panel preview
  // are too small to judge a crop by — the one thing someone wants after
  // uploading a picture is to see whether it actually looks right.
  const [zoom, setZoom] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // Escape closes it. A full-screen overlay with no keyboard exit is a trap,
  // and this one covers the whole page.
  useEffect(() => {
    if (!zoom) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setZoom(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);

  useEffect(() => {
    void getAvatar()
      .then((r) => setUrl(r.avatar_url))
      .catch(() => undefined);
  }, []);

  async function pick() {
    const file = fileRef.current?.files?.[0];
    if (!file || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const side = await shortSide(file);
      if (side !== null && side < MIN_SIDE_PX) {
        setErr(`That image is ${side}px on its shorter side; ${MIN_SIDE_PX}px or more looks sharp.`);
        return;
      }
      setUrl(await uploadAvatar(file));
      if (fileRef.current) fileRef.current.value = "";
      // The top-bar avatar is rendered by Shell, which read the URL on mount.
      // Reloading is blunt but honest — the alternative is a context just for
      // this, and the picture changes about once a year.
      window.location.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setErr(null);
    try {
      await clearAvatar();
      setUrl(null);
      window.location.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 18 }}>
      <h2 className="ui-h3">Profile picture</h2>
      <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
        <button
          type="button"
          onClick={() => url && setZoom(true)}
          aria-label={url ? "View your picture full size" : "No picture set"}
          disabled={!url}
          style={{
            border: 0,
            padding: 0,
            cursor: url ? "zoom-in" : "default",
            width: 120,
            height: 120,
            borderRadius: "50%",
            flex: "none",
            overflow: "hidden",
            display: "grid",
            placeItems: "center",
            background: url ? "var(--bg)" : "linear-gradient(135deg, var(--accent), #8f7bff)",
            color: "#fff",
            fontSize: 44,
            fontWeight: 600,
          }}
        >
          {url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={url}
              alt="Your profile picture"
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          ) : (
            email[0]?.toUpperCase()
          )}
        </button>

        <div style={{ flex: "1 1 240px", minWidth: 0 }}>
          <input
            ref={fileRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            disabled={busy}
            onChange={() => void pick()}
            style={{ display: "block", fontSize: 13, marginBottom: 8 }}
          />
          <p className="ui-sub" style={{ margin: 0 }}>
            {url
              ? "Shown in the top bar. Click it to see it full size, or choose a file to replace it."
              : `Optional. Without one the top bar shows “${email[0]?.toUpperCase()}”.`}
          </p>
        </div>

        {url ? (
          <button className="ui-btn" onClick={() => void remove()} disabled={busy}>
            {busy ? "…" : "Remove"}
          </button>
        ) : null}
      </div>

      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}

      {/* The full-size view. `contain`, not `cover`: this is the one place
          the whole photograph should be visible rather than cropped to a
          circle — the point is to check the crop, so cropping it again here
          would defeat it. */}
      {zoom && url ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Your profile picture"
          onClick={() => setZoom(false)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 50,
            background: "rgba(16,15,32,.72)",
            display: "grid",
            placeItems: "center",
            padding: 24,
            cursor: "zoom-out",
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt="Your profile picture"
            style={{
              maxWidth: "min(560px, 92vw)",
              maxHeight: "86vh",
              objectFit: "contain",
              borderRadius: 14,
              boxShadow: "0 20px 60px rgba(0,0,0,.4)",
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
