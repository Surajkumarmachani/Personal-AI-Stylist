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
import AddGarments from "../AddGarments";
import GarmentEditor from "../GarmentEditor";
import { listGarments, removeGarment, type Garment } from "@/lib/api";
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
  const [adding, setAdding] = useState(false);
  // The garment whose tags are open for correction. The editor lived only on
  // /dev, the build console — a FULL page load away, behind its own
  // email-and-password form because that page never calls `restoreSession`.
  // So the one screen showing a wrong tag was the one screen that could not
  // fix it, and the advice "correct it on the card" named an affordance that
  // did not exist. Same argument as AddGarments: a wardrobe you cannot
  // correct from the wardrobe screen is not finished.
  const [editing, setEditing] = useState<string | null>(null);
  // Two-step remove. The first click ARMS the button and the second confirms,
  // rather than a modal: the cards are small and sit under the thumb, and an
  // undo-less destructive action one tap away from a scroll is a mis-tap
  // waiting to happen. Only one card can be armed at a time.
  const [armed, setArmed] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
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

  async function remove(id: string) {
    setRemoving(id);
    setErr(null);
    try {
      await removeGarment(id);
      // Re-read rather than splicing locally: removing a garment also prunes
      // the precomputed outfits naming it, and the category counts above are
      // derived from the list.
      setItems(await listGarments());
    } catch (e) {
      setErr(String(e));
    } finally {
      setRemoving(null);
      setArmed(null);
    }
  }

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell email={email} back>
      <div className="ui-head">
        <div>
          <h1 style={{ margin: "0 0 4px", fontSize: 26, fontWeight: 640 }}>My Wardrobe</h1>
          <p className="ui-sub">Manage your clothes and get better recommendations.</p>
        </div>
        <button className="ui-btn primary" onClick={() => setAdding((v) => !v)}>
          {adding ? "Close" : "+ Add"}
        </button>
      </div>

      {adding ? (
        <AddGarments
          onDone={() => {
            // Re-read rather than optimistically inserting: the pipeline
            // decides how many garments a photo becomes (a flat-lay can
            // yield four), so the server is the only source of truth.
            listGarments().then(setItems).catch(() => undefined);
          }}
        />
      ) : null}

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

      {editing ? (
        <GarmentEditor
          garmentId={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            // Re-read: a slot change moves the garment between category tabs,
            // and the counts above are derived from the list.
            listGarments().then(setItems).catch(() => undefined);
          }}
        />
      ) : null}

      {shown.length === 0 ? (
        <div className="ui-empty">Nothing here yet. Add photos from the build console.</div>
      ) : (
        <div className="ui-grid tight">
          {shown.map((g, i) => (
            <article
              key={g.id}
              className="ui-card"
              style={{ animationDelay: `${i * 18}ms`, cursor: "pointer" }}
              onClick={() => setEditing(g.id)}
              title="Click to review and correct this item's tags"
            >
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
                {/* Named, not an icon. "Edit" next to the slot is what tells
                    the user the SLOT is the thing they can change — the field
                    most worth correcting, because a wrong slot silently
                    removes the garment from every outfit that needs it. */}
                <p className="ui-sub" style={{ fontSize: 11.5, opacity: 0.75 }}>
                  {g.slot ?? "slot unknown"} · Edit
                </p>
                {/* stopPropagation, or removing a garment also opens the
                    editor for the row that is about to disappear. */}
                <button
                  className="ui-btn"
                  style={{
                    marginTop: 8,
                    fontSize: 11.5,
                    padding: "4px 9px",
                    color: armed === g.id ? "var(--bad, #b4232a)" : "var(--ink-2)",
                    borderColor: armed === g.id ? "var(--bad, #b4232a)" : undefined,
                  }}
                  disabled={removing === g.id}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (armed === g.id) void remove(g.id);
                    else setArmed(g.id);
                  }}
                  onBlur={() => setArmed((a) => (a === g.id ? null : a))}
                >
                  {removing === g.id
                    ? "Removing…"
                    : armed === g.id
                      ? "Tap again to remove"
                      : "Remove"}
                </button>
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
