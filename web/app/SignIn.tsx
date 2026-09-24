"use client";

/** Sign-in, and sign-up. Rendered by every screen because the access token is
 *  in-memory only (lib/api.ts), so any route can be the first one loaded.
 *
 *  SIGN-UP ASKS ONE THING BEYOND THE LOGIN: whose clothes to suggest buying.
 *  It is the only answer the shop panel and the styling advice need that the
 *  wardrobe cannot supply, and asking later meant a new account's first
 *  shortfall offered womenswear and menswear side by side. See DressesAs.tsx. */

import { useState } from "react";
import { login, register, type DressesAs } from "@/lib/api";
import { DRESSES_AS_OPTIONS, DRESSES_AS_WHY } from "./DressesAs";
import { rememberEmail } from "./session";
import "./ui.css";

export default function SignIn({ onDone }: { onDone: (email: string) => void }) {
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [dressesAs, setDressesAs] = useState<DressesAs | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (mode === "signup" && !dressesAs) {
      setErr("Pick which clothes to suggest — “Both” is fine.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      // Both store the tokens themselves — see setSession in lib/api.ts.
      if (mode === "signup") await register(email, password, dressesAs ?? undefined);
      else await login(email, password);
      rememberEmail(email);
      onDone(email);
    } catch (e2) {
      setErr(String(e2));
    } finally {
      setBusy(false);
    }
  }

  const signup = mode === "signup";

  return (
    <div className="ui" style={{ display: "block" }}>
      <form className="ui-auth" onSubmit={submit}>
        <div className="ui-logo" style={{ padding: "0 0 8px" }}>
          <i aria-hidden="true" />
          AI Stylist
        </div>
        <input type="email" placeholder="email" value={email} autoComplete="username"
               onChange={(e) => setEmail(e.target.value)} required />
        <input type="password" placeholder={signup ? "password (12+ characters)" : "password"}
               value={password} autoComplete={signup ? "new-password" : "current-password"}
               minLength={signup ? 12 : undefined}
               onChange={(e) => setPassword(e.target.value)} required />

        {signup ? (
          <fieldset style={{ border: 0, padding: 0, margin: "4px 0 0" }}>
            <legend className="ui-h3" style={{ fontSize: 14, marginBottom: 6 }}>
              Which clothes should I suggest?
            </legend>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {DRESSES_AS_OPTIONS.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  className={`ui-btn${dressesAs === o.value ? " primary" : ""}`}
                  aria-pressed={dressesAs === o.value}
                  onClick={() => setDressesAs(o.value)}
                >
                  {o.label}
                </button>
              ))}
            </div>
            <p className="ui-sub" style={{ fontSize: 12, marginTop: 6 }}>
              {DRESSES_AS_WHY} You can change it in Profile.
            </p>
          </fieldset>
        ) : null}

        <button className="ui-btn primary" type="submit" disabled={busy}>
          {busy ? "…" : signup ? "Create account" : "Sign in"}
        </button>
        {err ? <p className="ui-err">{err}</p> : null}
        <button
          type="button"
          className="ui-btn"
          style={{ border: 0, background: "none" }}
          onClick={() => {
            setMode(signup ? "signin" : "signup");
            setErr(null);
          }}
        >
          {signup ? "Already have an account? Sign in" : "New here? Create an account"}
        </button>
      </form>
    </div>
  );
}
