"use client";

/** Occasion selection — the full taxonomy grid. */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Shell from "../Shell";
import SignIn from "../SignIn";
import { OCCASIONS } from "../OCCASIONS";
import { restoreSession } from "../session";
import "../ui.css";

export default function OccasionsPage() {
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const router = useRouter();
  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell email={email} back>
      <div style={{ textAlign: "center", marginBottom: 28 }}>
        <h1 style={{ margin: "0 0 6px", fontSize: 28, fontWeight: 640 }}>What&apos;s the occasion?</h1>
        <p className="ui-sub">Tell us the moment, we&apos;ll create the look.</p>
      </div>
      <div className="ui-grid tight">
        {OCCASIONS.map((o, i) => (
          <button
            key={o.id}
            className="ui-card"
            style={{ animationDelay: `${i * 30}ms`, textAlign: "left", padding: 0 }}
            onClick={() => router.push(`/explore?o=${encodeURIComponent(o.ask)}`)}
          >
            <div className="ui-frame" style={{ aspectRatio: "1 / 1" }}>
              <span style={{ fontSize: 24, opacity: 0.32 }} aria-hidden="true">◇</span>
            </div>
            <div className="ui-cbody">
              <span className="ui-name">{o.title}</span>
              <p className="ui-sub">{o.sub}</p>
            </div>
          </button>
        ))}
      </div>
      <div className="ui-unavailable" style={{ marginTop: 20 }}>
        <b>Custom occasions</b> aren&apos;t supported: an occasion has to exist in the taxonomy
        for the scorer to have formality and dress-code targets for it.
      </div>
    </Shell>
  );
}
