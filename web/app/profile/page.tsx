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
import SignIn from "../SignIn";
import { addPreference, listPreferences, type PreferenceFact } from "@/lib/api";
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

  async function prefer(colour: string) {
    setBusy(true);
    setErr(null);
    try {
      await addPreference("prefers", "primary_colour", colour);
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
      </div>

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
              onClick={() => void prefer(c)}
            >
              {c.replace(/_/g, " ")}
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
