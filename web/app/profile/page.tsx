"use client";

/** Profile & preferences.
 *
 * Style preferences are REAL: `preference_fact` rows with kind
 * prefers/avoids/never, which the suggest pipeline actually reads — an
 * `avoids` fact hides garments from the pool and a `never` fact excludes them
 * outright. So toggling a colour here changes tomorrow's suggestions.
 *
 * `source` is shown because the table records whether a fact came from the
 * user or was inferred, and presenting a guess as the user's own words is how
 * you lose their trust in one screen.
 *
 * Body measurements, saved addresses and payment methods have no tables and
 * no endpoints; they are named as absent rather than drawn as empty forms.
 */

import { useEffect, useState } from "react";
import Shell from "../Shell";
import AvatarUpload from "../AvatarUpload";
import HomeCity from "../HomeCity";
import CalendarConnect from "../CalendarConnect";
import PushNotifications from "../PushNotifications";
import PrivacyControls from "../PrivacyControls";
import SignIn from "../SignIn";
import {
  addPreference,
  deletePreference,
  listPreferences,
  signOut,
  type PreferenceFact,
} from "@/lib/api";
import { restoreSession } from "../session";
import "../ui.css";

const COLOURS = ["black", "white", "maroon", "blue_navy", "beige", "emerald", "rani_pink", "mustard"];

export default function ProfilePage() {
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const [facts, setFacts] = useState<PreferenceFact[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);

  function refresh() {
    listPreferences().then((r) => setFacts(r.facts)).catch((e) => setErr(String(e)));
  }
  useEffect(() => {
    if (email) refresh();
  }, [email]);

  /** Toggle, not add-only.
   *
   * These chips looked like a toggle — tapping one filled it in — and only
   * ever ADDED a preference fact. There was no way to undo a mis-tap from
   * this screen, and the facts are not decoration: the suggest pipeline reads
   * `prefers` when it ranks, so a colour added by accident kept steering
   * every suggestion with nothing on screen to take it back.
   *
   * The delete endpoint already existed and nothing called it.
   */
  async function toggleColour(colour: string) {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const existing = facts.find(
        (f) =>
          f.kind === "prefers" && f.field_name === "primary_colour" && f.field_value === colour,
      );
      if (existing) await deletePreference(existing.id);
      else await addPreference("prefers", "primary_colour", colour);
      refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  const chosen = new Set(
    facts.filter((f) => f.kind === "prefers" && f.field_name === "primary_colour").map((f) => f.field_value),
  );

  return (
    <Shell email={email} back>
      <h1 style={{ margin: "0 0 20px", fontSize: 26, fontWeight: 640 }}>My Profile</h1>

      <div className="ui-panel" style={{ marginBottom: 18 }}>
        <h2 className="ui-h3">Account</h2>
        <p className="ui-sub">{email}</p>
        {/* SIGN OUT, which did not exist anywhere in the app. The only way to
            leave an account was to delete it — the one button that was
            offered — or to clear browser storage by hand. `signOut` revokes
            the refresh token server-side as well as clearing it locally;
            forgetting it in this browser alone would leave a valid token in
            anyone else's hands. */}
        <button
          className="ui-btn"
          style={{ marginTop: 10 }}
          onClick={() => {
            void signOut().then(() => {
              // A full reload, not a route push: every screen holds its own
              // in-memory state from the account being left. Same reasoning
              // as account deletion in PrivacyControls.
              window.location.href = "/";
            });
          }}
        >
          Sign out
        </button>
      </div>

      <AvatarUpload email={email} />

      <div style={{ marginBottom: 18 }}>
        <HomeCity />
      </div>

      <CalendarConnect />

      <PushNotifications />

      <PrivacyControls email={email} />

      <div className="ui-panel" style={{ marginBottom: 18 }}>
        <h2 className="ui-h3">Preferred colours</h2>
        <p className="ui-sub" style={{ marginBottom: 12 }}>
          These are real: the suggest pipeline reads them, so a choice here changes tomorrow&apos;s looks.
        </p>
        <div className="ui-pills">
          {COLOURS.map((c) => (
            <button
              key={c}
              className={`ui-pill${chosen.has(c) ? " on" : ""}`}
              disabled={busy}
              aria-pressed={chosen.has(c)}
              title={chosen.has(c) ? `Tap to stop preferring ${c.replace(/_/g, " ")}` : undefined}
              onClick={() => void toggleColour(c)}
            >
              {c.replace(/_/g, " ")}
              {chosen.has(c) ? <span aria-hidden="true"> ×</span> : null}
            </button>
          ))}
        </div>
        {err ? <p className="ui-err">{err}</p> : null}
      </div>

      <div className="ui-panel" style={{ marginBottom: 18 }}>
        <h2 className="ui-h3">Your preference facts ({facts.length})</h2>
        {facts.length === 0 ? (
          <p className="ui-sub">None yet.</p>
        ) : (
          <div className="ui-pills">
            {facts.map((f) => (
              <span key={f.id} className="ui-tag" style={{ fontSize: 12, padding: "5px 9px" }}>
                {f.kind} · {f.field_name.replace(/_/g, " ")} = {f.field_value}
                {f.source === "inferred" ? " (inferred)" : ""}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="ui-unavailable">
        <b>Body measurements, saved addresses and payment methods are not built.</b> There are no
        tables and no endpoints for any of them, so they are named here rather than drawn as forms
        that would discard whatever you typed.
      </div>
    </Shell>
  );
}
