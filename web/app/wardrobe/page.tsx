"use client";

/** My Wardrobe — Tops / Bottoms / Dresses / Outerwear / Shoes / Accessories.
 *
 * The categories are DERIVED from taxonomy slots rather than stored on the
 * garment. A new slot in taxonomy.yaml then lands somewhere sensible instead
 * of disappearing from this screen, and there is no second source of truth to
 * drift from the first.
 */

import { useEffect, useMemo, useState } from "react";
import Shell from "../Shell";
import SignIn from "../SignIn";
import { listGarments, type Garment } from "@/lib/api";
import { restoreSession } from "../session";
import "../ui.css";

const TABS = [
  { key: "all", label: "All" },
  { key: "tops", label: "Tops" },
  { key: "bottoms", label: "Bottoms" },
  { key: "dresses", label: "Dresses" },
  { key: "outerwear", label: "Outerwear" },
  { key: "shoes", label: "Shoes" },
  { key: "accessories", label: "Accessories" },
];

function categoryOf(slot: string | null): string {
  switch (slot) {
    case "upper_base": return "tops";
    case "lower": return "bottoms";
    case "full_body": case "drape": return "dresses";
    case "upper_layer": return "outerwear";
    case "feet": return "shoes";
    case "bag": case "accessory": case "head": return "accessories";
    default: return "tops";
  }
}

export default function WardrobePage() {
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const [items, setItems] = useState<Garment[]>([]);
  const [tab, setTab] = useState("all");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    if (email) listGarments().then(setItems).catch((e) => setErr(String(e)));
  }, [email]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length };
    for (const g of items) {
      const k = categoryOf(g.slot);
      c[k] = (c[k] ?? 0) + 1;
    }
    return c;
  }, [items]);

  const shown = useMemo(
    () => (tab === "all" ? items : items.filter((g) => categoryOf(g.slot) === tab)),
    [items, tab],
  );

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell email={email} back>
      <div className="ui-head">
        <div>
          <h1 style={{ margin: "0 0 4px", fontSize: 26, fontWeight: 640 }}>My Wardrobe</h1>
          <p className="ui-sub">Manage your clothes and get better recommendations.</p>
        </div>
        <a className="ui-btn primary" href="/dev">+ Add</a>
      </div>

      <div className="ui-pills" style={{ marginBottom: 22 }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`ui-pill${tab === t.key ? " on" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {counts[t.key] ? ` (${counts[t.key]})` : ""}
          </button>
        ))}
      </div>

      {err ? <p className="ui-err">{err}</p> : null}

      {shown.length === 0 ? (
        <div className="ui-empty">Nothing here yet. Add photos from the build console.</div>
      ) : (
        <div className="ui-grid tight">
          {shown.map((g, i) => (
            <article key={g.id} className="ui-card" style={{ animationDelay: `${i * 18}ms` }}>
              <div className="ui-frame one" style={{ aspectRatio: "1 / 1" }}>
                {g.cutout_url ? (
                  <img src={g.cutout_url} alt={g.subcategory ?? "garment"} loading="lazy" />
                ) : (
                  <span className="ui-ph">{g.state}</span>
                )}
              </div>
              <div className="ui-cbody">
                <span className="ui-name" style={{ fontSize: 13.5 }}>
                  {g.subcategory ?? g.slot ?? "unidentified"}
                </span>
                <p className="ui-sub">{g.primary_colour ?? "colour pending"}</p>
              </div>
            </article>
          ))}
        </div>
      )}

      <div className="ui-unavailable" style={{ marginTop: 22 }}>
        <b>No sizes or brands.</b> The taxonomy records slot, colour, pattern, material, fit and
        formality — it has no size or brand field, so neither is shown rather than invented.
      </div>
    </Shell>
  );
}
