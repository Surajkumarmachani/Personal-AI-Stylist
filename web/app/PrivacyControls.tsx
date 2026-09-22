"use client";

/** Export your data, or delete the account.
 *
 * WHY THIS IS NOT OPTIONAL
 * The erasure saga (§C5) is built, audited and has a 30-day SLA — and there
 * was no way for a user to start it. Under DPDP 2023 and GDPR Art. 17 the
 * right to erasure has to be exercisable BY THE USER, so a working backend
 * with no button is a compliance gap, not a missing nice-to-have. Same for
 * export and the right of access.
 *
 * THE CONFIRMATION IS TYPED, NOT TAPPED
 * The API requires `?confirm=DELETE` and its own comment says the endpoint is
 * "one stray request away from deleting an account". The obvious client
 * shortcut is to send that word on the user's behalf behind a single button,
 * which converts a deliberate act into a mis-tap. So the word is typed here
 * too. This is the one screen in the app where friction is the feature.
 */

import { useState } from "react";
import { deleteAccount, exportStatus, requestExport, clearSession } from "@/lib/api";

export default function PrivacyControls({ email }: { email: string }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState("");
  const [showDelete, setShowDelete] = useState(false);

  async function startExport() {
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const { export_id } = await requestExport();
      setNote("Preparing your export…");
      // The worker builds it asynchronously, so the UI polls rather than
      // claiming it is ready. A "download" link that 404s is worse than a wait.
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const s = await exportStatus(export_id);
        if (s.state === "ready") {
          setNote(
            s.download_url
              ? "Your export is ready — the link is valid for a short time."
              : "Your export is ready.",
          );
          if (s.download_url) window.open(s.download_url, "_blank", "noopener");
          return;
        }
        if (s.state === "failed") {
          setErr(s.last_error ?? "The export failed.");
          return;
        }
      }
      setNote("Still being prepared. Come back in a few minutes.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reallyDelete() {
    if (confirm !== "DELETE" || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await deleteAccount();
      clearSession();
      // A full reload, not a route push: every screen gates on an in-memory
      // token, and the account this one belongs to no longer exists.
      window.location.href = "/";
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 18 }}>
      <h2 className="ui-h3">Your data</h2>
      <p className="ui-sub" style={{ marginBottom: 12 }}>
        Everything held for <b style={{ color: "var(--ink)" }}>{email}</b>: garments, photos,
        wear history, feedback and the taste vector built from it.
      </p>

      <button className="ui-btn" onClick={() => void startExport()} disabled={busy}>
        {busy ? "Working…" : "Download my data"}
      </button>

      <div style={{ marginTop: 18, paddingTop: 14, borderTop: "1px solid var(--line)" }}>
        <h2 className="ui-h3">Delete this account</h2>
        <p className="ui-sub" style={{ marginBottom: 10 }}>
          Removes your garment rows, every photograph and every version of it in storage, the
          derived vectors and the provider caches. It cannot be undone, and it starts
          immediately — sign-in stops working straight away while the rest completes within 30
          days.
        </p>

        {!showDelete ? (
          <button className="ui-btn" onClick={() => setShowDelete(true)} disabled={busy}>
            Delete my account…
          </button>
        ) : (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <input
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Type DELETE to confirm"
              aria-label="Type DELETE to confirm"
              disabled={busy}
              style={{ flex: "1 1 200px", fontSize: 13 }}
            />
            <button
              className="ui-btn"
              style={{ color: "var(--bad, #b4232a)", borderColor: "var(--bad, #b4232a)" }}
              disabled={busy || confirm !== "DELETE"}
              onClick={() => void reallyDelete()}
            >
              {busy ? "Deleting…" : "Delete permanently"}
            </button>
            <button className="ui-btn" onClick={() => setShowDelete(false)} disabled={busy}>
              Cancel
            </button>
          </div>
        )}
      </div>

      {note ? <p className="ui-sub" style={{ color: "var(--ok)", marginTop: 10 }}>{note}</p> : null}
      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}
    </div>
  );
}
