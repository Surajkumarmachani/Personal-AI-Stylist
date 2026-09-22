"use client";

/** Body photo + consent.
 *
 * THE MOST SENSITIVE THING THIS PRODUCT STORES, and the flow reflects that.
 *
 * Consent is a SEPARATE, EXPLICIT ACT, not a consequence of choosing a file.
 * The server rejects `consent_to_virtual_tryon: false`, and this UI matches
 * that: the checkbox is unticked by default and the upload button stays
 * disabled until it is ticked. "They picked a file, so they must have agreed"
 * is the reasoning that makes consent a formality.
 *
 * The NOTICE IS THE SERVER'S WORDS, rendered verbatim rather than paraphrased.
 * It names the third party the photo would be transmitted to when a provider
 * is configured, and consent to storage is not consent to transmission — a
 * paraphrase here could quietly drop the part that matters.
 *
 * Revocation is one click and is offered in the same place as the upload. §C5
 * requires consent be withdrawable without deleting the account, so it must
 * not be buried in a settings screen.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  listBodyPhotos,
  presignBodyPhoto,
  recordBodyPhoto,
  revokeBodyPhotos,
  uploadBodyPhoto,
  type BodyPhoto as Photo,
} from "@/lib/api";

export default function BodyPhotoPanel({
  onChange,
}: {
  // (consented photos, third party the photo would go to or null).
  // The SECOND value is how the caller learns whether a VTON provider is
  // configured at all — the server only names a third party when one is.
  onChange?: (active: number, thirdParty: string | null) => void;
}) {
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [active, setActive] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [thirdParty, setThirdParty] = useState<string | null>(null);
  const [consented, setConsented] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // A ref, not the state: `refresh` is memoised and would capture a stale
  // `thirdParty` if it read the state directly.
  const thirdPartyRef = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await listBodyPhotos();
      setPhotos(r.photos);
      setActive(r.active);
      onChange?.(r.active, thirdPartyRef.current);
    } catch (e) {
      setErr(String(e));
    }
  }, [onChange]);

  useEffect(() => {
    void refresh();
    // Fetch the notice up front so the user reads what they are agreeing to
    // BEFORE choosing a file, not after.
    presignBodyPhoto()
      .then((p) => {
        setNotice(p.notice);
        setThirdParty(p.sent_to_third_party);
        thirdPartyRef.current = p.sent_to_third_party;
        void refresh();
      })
      .catch(() => setNotice(null));
  }, [refresh]);

  async function upload() {
    const file = fileRef.current?.files?.[0];
    if (!file || !consented) return;
    setBusy(true);
    setErr(null);
    setOk(null);
    try {
      // A FRESH presign per upload: the one fetched for the notice may have
      // expired while the user was reading it, and reusing an expired policy
      // fails at the storage layer with an error that says nothing useful.
      const p = await presignBodyPhoto();
      await uploadBodyPhoto(p, file);
      await recordBodyPhoto(p);
      setOk("Body photo saved. Try-on can use it once a provider is configured.");
      setConsented(false);
      if (fileRef.current) fileRef.current.value = "";
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    setBusy(true);
    setErr(null);
    setOk(null);
    try {
      await revokeBodyPhotos();
      setOk("Consent withdrawn and the photo deleted. Your account is untouched.");
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 22 }}>
      <h2 className="ui-h3">Body photo</h2>

      {active > 0 ? (
        <>
          <p className="ui-sub" style={{ marginBottom: 12 }}>
            ✓ {active} photo{active > 1 ? "s" : ""} stored with your consent. Try-on will use the
            most recent one.
          </p>
          <button className="ui-btn" onClick={() => void revoke()} disabled={busy}>
            {busy ? "…" : "Withdraw consent & delete"}
          </button>
          <p className="ui-sub" style={{ marginTop: 8 }}>
            This deletes the photo. It does not delete your account.
          </p>
        </>
      ) : (
        <>
          <p className="ui-sub" style={{ marginBottom: 12 }}>
            Needed before any try-on can be rendered. Upload a clear, full-length photo of
            yourself.
          </p>

          {/* The server's own wording, verbatim. */}
          {notice ? (
            <div className="ui-unavailable" style={{ marginBottom: 12 }}>
              {notice}
              {thirdParty ? (
                <>
                  {" "}
                  <b>This photo would be sent to {thirdParty}.</b>
                </>
              ) : null}
            </div>
          ) : null}

          <input ref={fileRef} type="file" accept="image/jpeg,image/png" disabled={busy}
                 style={{ display: "block", marginBottom: 12, fontSize: 13 }} />

          {/* Unticked by default, and the button is dead until it is ticked. */}
          <label style={{ display: "flex", gap: 9, alignItems: "flex-start", fontSize: 13.5, marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={consented}
              onChange={(e) => setConsented(e.target.checked)}
              disabled={busy}
              style={{ marginTop: 3 }}
            />
            <span>
              I consent to this photo being stored and used to render virtual try-on images
              {thirdParty ? `, including being sent to ${thirdParty}` : ""}. I can withdraw this
              at any time.
            </span>
          </label>

          <button className="ui-btn primary" onClick={() => void upload()} disabled={busy || !consented}>
            {busy ? "Uploading…" : "Upload body photo"}
          </button>
          {!consented ? (
            <p className="ui-sub" style={{ marginTop: 8 }}>Tick the box to enable upload.</p>
          ) : null}
        </>
      )}

      {ok ? <p className="ui-sub" style={{ color: "var(--ok)", marginTop: 10 }}>{ok}</p> : null}
      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}

      {photos.length > 0 ? (
        <p className="ui-sub" style={{ marginTop: 12 }}>
          {photos.length} record{photos.length > 1 ? "s" : ""} —{" "}
          {photos.filter((p) => p.revoked_at).length} revoked. The image key is never shown; it
          points at the most sensitive object stored here.
        </p>
      ) : null}
    </div>
  );
}
