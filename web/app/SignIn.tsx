"use client";

/** Sign-in. Rendered by every screen because the access token is in-memory
 *  only (lib/api.ts), so any route can be the first one loaded. */

import { useState } from "react";
import { login } from "@/lib/api";
import { rememberEmail } from "./session";
import "./ui.css";

export default function SignIn({ onDone }: { onDone: (email: string) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      // `login` stores both tokens itself — see setSession in lib/api.ts.
      await login(email, password);
      rememberEmail(email);
      onDone(email);
    } catch (e2) {
      setErr(String(e2));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ui" style={{ display: "block" }}>
      <form className="ui-auth" onSubmit={submit}>
        <div className="ui-logo" style={{ padding: "0 0 8px" }}>
          <i aria-hidden="true" />
          AI Stylist
        </div>
        <input type="email" placeholder="email" value={email} autoComplete="username"
               onChange={(e) => setEmail(e.target.value)} required />
        <input type="password" placeholder="password" value={password} autoComplete="current-password"
               onChange={(e) => setPassword(e.target.value)} required />
        <button className="ui-btn primary" type="submit" disabled={busy}>
          {busy ? "…" : "Sign in"}
        </button>
        {err ? <p className="ui-err">{err}</p> : null}
      </form>
    </div>
  );
}
