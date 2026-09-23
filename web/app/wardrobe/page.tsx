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
import {
  listGarments,
  logWear,
  pendingDuplicates,
  removeGarment,
  resolveDuplicate,
  setLaundry,
  type DuplicatePair,
  type Garment,
} from "@/lib/api";
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
  // Free-text filter over what is already loaded. NOT `GET /wardrobe/search`:
  // that endpoint does semantic search across the whole wardrobe and belongs
  // on a screen of its own; this is the "where is my black shirt" filter on a
  // grid the user is already looking at, and a round trip per keystroke would
  // be slower and worse.
  const [q, setQ] = useState("");
  const [dupes, setDupes] = useState<DuplicatePair[]>([]);
  const [acting, setActing] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    if (!email) return;
    listGarments().then(setItems).catch((e) => setErr(String(e)));
    // The pipeline flags near-identical uploads and parks them as
    // `duplicate_suspect`, which EXCLUDES them from every suggestion until
    // someone resolves the pair. That queue only existed in the build
    // console, so a user's wardrobe could quietly hold items nothing would
    // ever suggest.
    pendingDuplicates().then((r) => setDupes(r.items)).catch(() => undefined);
  }, [email]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length };
    for (const g of items) {
      const k = categoryOf(g.slot);
      c[k] = (c[k] ?? 0) + 1;
    }
    return c;
  }, [items]);

  const shown = useMemo(() => {
    const byTab = tab === "all" ? items : items.filter((g) => categoryOf(g.slot) === tab);
    const needle = q.trim().toLowerCase();
    if (!needle) return byTab;
    // Matches subcategory, colour and slot — the three things written on a
    // card — so what the user searches for is what they can see.
    return byTab.filter((g) =>
      // Brand and size are searchable BECAUSE they are on the card: what the
      // user can read, the filter should match.
      [g.subcategory, g.primary_colour, g.slot, g.brand, g.size_label]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().replace(/_/g, " ").includes(needle)),
    );
  }, [items, tab, q]);

  async function wore(g: Garment) {
    if (acting) return;
    setActing(g.id);
    setErr(null);
    try {
      await logWear(g.id);
      setToast(`Logged a wear for ${g.subcategory ?? "that"}.`);
      setItems(await listGarments());
    } catch (e) {
      setErr(String(e));
    } finally {
      setActing(null);
    }
  }

  async function toggleWash(g: Garment) {
    if (acting) return;
    setActing(g.id);
    setErr(null);
    try {
      await setLaundry(g.id, !g.needs_wash);
      setItems(await listGarments());
    } catch (e) {
      setErr(String(e));
    } finally {
      setActing(null);
    }
  }

  async function resolve(id: string, resolution: "same" | "different") {
    setActing(id);
    try {
      await resolveDuplicate(id, resolution);
      setDupes((d) => d.filter((x) => x.garment_id !== id));
      setItems(await listGarments());
    } catch (e) {
      setErr(String(e));
    } finally {
      setActing(null);
    }
  }

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

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Filter — black shirt, Levi\u2019s, size M…"
        aria-label="Filter your wardrobe"
        style={{ width: "100%", marginBottom: 12, fontSize: 13 }}
      />

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

      {dupes.length > 0 ? (
        <div className="ui-panel" style={{ marginBottom: 18 }}>
          <h2 className="ui-h3">
            {dupes.length} possible duplicate{dupes.length > 1 ? "s" : ""}
          </h2>
          <p className="ui-sub" style={{ marginBottom: 10 }}>
            These are parked as <code>duplicate_suspect</code>, which excludes them from every
            suggestion until you say. Resolving them is the only way they come back.
          </p>
          {dupes.map((d) => (
            <div
              key={d.garment_id}
              style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}
            >
              <span className="ui-sub">
                <b style={{ color: "var(--ink)" }}>{d.subcategory ?? "item"}</b>{" "}
                ({d.primary_colour ?? "?"}) vs {d.duplicate_of?.subcategory ?? "an earlier item"}
              </span>
              <button
                className="ui-btn"
                disabled={acting === d.garment_id}
                onClick={() => void resolve(d.garment_id, "different")}
              >
                Different items
              </button>
              <button
                className="ui-btn"
                disabled={acting === d.garment_id}
                onClick={() => void resolve(d.garment_id, "same")}
                title="Merges the wear history onto the original and retires this one"
              >
                Same thing — merge
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {toast ? <p className="ui-sub" style={{ color: "var(--ok)" }}>{toast}</p> : null}
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
                {/* `<img>` not `next/image`: `cutout_url` is a presigned
                    MinIO URL whose signature expires and whose query string
                    changes every request, so the optimizer would re-encode on
                    every render and cache output that outlives its own URL. */}
                {g.cutout_url ? (
                  // eslint-disable-next-line @next/next/no-img-element -- presigned URL; see above
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
                {/* Shown only when set. An empty "brand: —" on every card is
                    noise for the many wardrobes that will never fill these in. */}
                {g.brand || g.size_label ? (
                  <p className="ui-sub" style={{ fontSize: 11.5 }}>
                    {[g.brand, g.size_label && `size ${g.size_label}`]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                ) : null}
                {/* Named, not an icon. "Edit" next to the slot is what tells
                    the user the SLOT is the thing they can change — the field
                    most worth correcting, because a wrong slot silently
                    removes the garment from every outfit that needs it. */}
                <p className="ui-sub" style={{ fontSize: 11.5, opacity: 0.75 }}>
                  {g.slot ?? "slot unknown"} · Edit
                </p>
                {/* THE THREE ACTIONS THAT WERE ONLY IN THE BUILD CONSOLE.
                    Each stops propagation, or it would also open the tag
                    editor for the card being acted on. */}
                <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                  <button
                    className="ui-btn"
                    style={{ fontSize: 11.5, padding: "4px 9px" }}
                    disabled={acting === g.id}
                    title="Log that you wore this today — feeds novelty and your style vector"
                    onClick={(e) => {
                      e.stopPropagation();
                      void wore(g);
                    }}
                  >
                    {acting === g.id ? "…" : "Wore it"}
                    {g.wear_count ? ` · ${g.wear_count}` : ""}
                  </button>
                  <button
                    className="ui-btn"
                    style={{
                      fontSize: 11.5,
                      padding: "4px 9px",
                      color: g.needs_wash ? "var(--bad, #b4232a)" : undefined,
                    }}
                    disabled={acting === g.id}
                    aria-pressed={g.needs_wash}
                    title={
                      g.needs_wash
                        ? "In the wash — excluded from every suggestion. Tap when clean."
                        : "Mark as in the wash"
                    }
                    onClick={(e) => {
                      e.stopPropagation();
                      void toggleWash(g);
                    }}
                  >
                    {g.needs_wash ? "In the wash" : "Wash"}
                  </button>
                </div>
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

      {/* This used to say sizes and brands did not exist. They do now — but
          the honest half of that old note still holds and is kept: they are
          NOT guessed from your photos, and they do not steer suggestions. */}
      <div className="ui-unavailable" style={{ marginTop: 22 }}>
        <b>Brand and size are yours to fill in.</b> Tap a garment to add them. They are never
        read from a photo — a logo guessed wrong is worse than a blank — and they do not affect
        which outfits are suggested, only what you can search and see.
      </div>
    </Shell>
  );
}
