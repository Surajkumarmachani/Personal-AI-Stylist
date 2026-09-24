"use client";

/** Whose clothes to suggest buying (migration 0026).
 *
 * WHY IT IS ASKED AT ALL. When the wardrobe cannot dress an occasion, the
 * shop panel and the styling advice reach past it — and without this they
 * offered a lehenga and a sherwani side by side to the same person.
 *
 * ASKED AS A CLOTHING QUESTION, NOT AN IDENTITY ONE, and "Both" is a real
 * answer. It never filters the user's own wardrobe: whatever they own, they
 * own, and every look is still built from it.
 *
 * `prompt` is the one-time question on the home screen for accounts made
 * before sign-up asked; it renders nothing once answered. `setting` is the
 * Profile panel, always shown so the answer can be changed.
 */

import { useEffect, useState } from "react";
import { getDressesAs, setDressesAs, type DressesAs as Value } from "@/lib/api";

export const DRESSES_AS_OPTIONS: { value: Value; label: string }[] = [
  { value: "women", label: "Women's clothing" },
  { value: "men", label: "Men's clothing" },
  { value: "all", label: "Both" },
];

export const DRESSES_AS_WHY =
  "Only used to pick what to suggest buying when your wardrobe can't dress an occasion. " +
  "Your own clothes are never filtered.";

export default function DressesAs({ variant }: { variant: "prompt" | "setting" }) {
  const [current, setCurrent] = useState<Value | null>(null);
  const [asked, setAsked] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void getDressesAs()
      .then((r) => {
        setCurrent(r.dresses_as);
        setAsked(r.asked);
      })
      .catch(() => setAsked(null));
  }, []);

  async function choose(value: Value) {
    setBusy(true);
    setErr(null);
    try {
      await setDressesAs(value);
      setCurrent(value);
      setAsked(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // Unknown (request failed) or already answered: the prompt stays out of the way.
  if (variant === "prompt" && asked !== false) return null;

  return (
    <div className="ui-panel" style={variant === "prompt" ? { marginBottom: 24 } : undefined}>
      <h2 className="ui-h3">Which clothes should I suggest?</h2>
      <p className="ui-sub" style={{ marginBottom: 12 }}>{DRESSES_AS_WHY}</p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }} role="group" aria-label="Which clothes">
        {DRESSES_AS_OPTIONS.map((o) => (
          <button
            key={o.value}
            className={`ui-btn${current === o.value ? " primary" : ""}`}
            aria-pressed={current === o.value}
            disabled={busy}
            onClick={() => void choose(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
      {err ? <p className="ui-err">{err}</p> : null}
    </div>
  );
}
