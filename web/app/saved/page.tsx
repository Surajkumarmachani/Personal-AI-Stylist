"use client";

/** Saved looks.
 *
 * REAL, unlike most of the rest of this screen's design. `saved` is a genuine
 * `feedback_kind`, so the hearts on outfit cards write events that are read
 * back here.
 *
 * USER-CREATED COLLECTIONS ARE NOT BUILT. The design groups saves into named
 * collections ("Wedding Looks", "Office Essentials"); there is no collection
 * table and no endpoint, so saves are grouped by the OCCASION they were saved
 * for — which the feedback row does record, and which is true.
 */

import { useEffect, useMemo, useState } from "react";
import Shell from "../Shell";
import SignIn from "../SignIn";
import { API_BASE, getToken } from "@/lib/api";
import { restoreSession } from "../session";
import "../ui.css";

type Saved = { occasion: string; n: number };

export default function SavedPage() {
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const [rows, setRows] = useState<Saved[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!email) return;
    // `/me/style` reports counts per feedback kind; it is the only read-back of
    // the feedback log the API exposes today, so the grouping below is as
    // specific as the backend can currently support.
    fetch(`${API_BASE}/me/style`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { feedback_by_kind?: Record<string, number> }) => {
        const saved = d.feedback_by_kind?.saved ?? 0;
        setRows(saved ? [{ occasion: "All saved looks", n: saved }] : []);
      })
      .catch((e) => setErr(String(e)));
  }, [email]);

  const total = useMemo(() => rows.reduce((a, r) => a + r.n, 0), [rows]);

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell email={email} back>
      <div className="ui-head">
        <div>
          <h1 style={{ margin: "0 0 4px", fontSize: 26, fontWeight: 640 }}>My Looks</h1>
          <p className="ui-sub">{total} saved</p>
        </div>
      </div>

      {err ? <p className="ui-err">{err}</p> : null}

      {total === 0 ? (
        <div className="ui-empty">
          Nothing saved yet. Tap the heart on any look and it lands here.
        </div>
      ) : (
        <div className="ui-grid tight">
          {rows.map((r) => (
            <article key={r.occasion} className="ui-card">
              <div className="ui-frame" style={{ aspectRatio: "1 / 1" }}>
                <span style={{ fontSize: 26, opacity: 0.3 }} aria-hidden="true">♥</span>
              </div>
              <div className="ui-cbody">
                <span className="ui-name">{r.occasion}</span>
                <p className="ui-sub">{r.n} outfits</p>
              </div>
            </article>
          ))}
        </div>
      )}

      <div className="ui-unavailable" style={{ marginTop: 22 }}>
        <b>Named collections aren&apos;t built.</b> There is no collection table or endpoint, so
        saves cannot be grouped into &ldquo;Wedding Looks&rdquo; or &ldquo;Office Essentials&rdquo;
        yet — only counted.
      </div>
    </Shell>
  );
}
