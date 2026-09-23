"use client";

/** Try On.
 *
 * WHAT WENT WRONG IN THE FIRST VERSION, recorded because it is a UI failure
 * rather than a backend one: this screen hardcoded a single "casual" query. An
 * occasion that yields nothing then left a dead page showing one bare sentence
 * — "no wearable feet" — with no way to try anything else and no explanation of
 * what "wearable" excluded.
 *
 * The backend message was RIGHT. `feet` is the only strictly required slot, so
 * a wardrobe with no eligible footwear genuinely cannot produce an outfit. The
 * bug was showing that as a dead end.
 *
 * Now: the occasion is selectable, and when nothing comes back the screen says
 * which slot is missing and links to where it can be fixed.
 *
 * LIVE AR (3D) IS STILL NOT A CONTROL. There is no AR pipeline in this system
 * at all, and a toggle that does nothing is worse than its absence.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import Shell from "../Shell";
import SignIn from "../SignIn";
import OutfitCard from "../OutfitCard";
import BodyPhotoPanel from "../BodyPhoto";
import { OCCASIONS } from "../OCCASIONS";
import { askStylist, listGarments, type ChatOutfit, type Garment } from "@/lib/api";
import { restoreSession } from "../session";
import "../ui.css";

/** Slots an outfit cannot be built without.
 *
 * Mirrors `stylist_domain.slots.required_slots()` plus `base_structures()`.
 * Duplicated here rather than fetched because it drives a HINT, not a
 * decision — the backend remains the authority on whether an outfit is valid,
 * and this only explains an empty result the backend already returned. */
const NEEDS = [
  { slots: ["feet"], label: "footwear", why: "every outfit needs shoes" },
  {
    slots: ["upper_base", "full_body"],
    label: "a top or a full-body piece",
    why: "an outfit is a top and bottom, or a dress/kurta",
  },
  { slots: ["lower", "full_body"], label: "a bottom or a full-body piece", why: "" },
];

export default function TryOnPage() {
  const [email, setEmail] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [outfits, setOutfits] = useState<ChatOutfit[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [occasion, setOccasion] = useState("casual");
  const [wardrobe, setWardrobe] = useState<Garment[] | null>(null);
  const [consented, setConsented] = useState(0);
  // Non-null when the server names a third party, which it only does when
  // a VTON provider is actually configured. This is REPORTED state, not an
  // assumption about tokens — see the notice below.
  const [provider, setProvider] = useState<string | null>(null);

  useEffect(() => {
    void restoreSession().then(setEmail).finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (email) listGarments().then(setWardrobe).catch(() => setWardrobe(null));
  }, [email]);

  // Inlined instead of calling a memoised `load()`: React's set-state-in-effect
  // rule cannot see through a callback, so it flags every effect that invokes
  // one. Inlining also buys the thing that was genuinely missing —
  // CANCELLATION. Nothing tracked which request was current, so switching
  // occasion quickly left whichever response landed last on screen, which is
  // not necessarily the one that was asked for. `off` makes an abandoned
  // request's reply a no-op instead of a race.
  useEffect(() => {
    if (!email) return;
    let off = false;
    void (async () => {
      setBusy(true);
      setNote(null);
      try {
        const res = await askStylist(occasion, 6);
        if (off) return;
        setOutfits(res.outfits);
        if (!res.outfits.length) setNote(res.notes?.[0] ?? res.reply);
      } catch (e) {
        if (off) return;
        setNote(String(e));
        setOutfits([]);
      } finally {
        if (!off) setBusy(false);
      }
    })();
    return () => {
      off = true;
    };
  }, [email, occasion]);

  /** Which requirement the wardrobe cannot meet AT ALL — ignoring laundry,
   *  weather and dress code, which the backend's own note already covers. A
   *  wardrobe with zero shoes is a different problem from one whose shoes are
   *  all in the wash, and conflating them sends the user to the wrong place. */
  const missing = useMemo(() => {
    if (!wardrobe) return [];
    const have = new Set(wardrobe.map((g) => g.slot).filter(Boolean) as string[]);
    return NEEDS.filter((n) => !n.slots.some((s) => have.has(s)));
  }, [wardrobe]);

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell email={email} back>
      <h1 style={{ margin: "0 0 4px", fontSize: 26, fontWeight: 640 }}>Try it on</h1>
      <p className="ui-sub" style={{ marginBottom: 18 }}>
        Pick an occasion, then try any look on.
      </p>

      <div className="ui-pills" style={{ marginBottom: 16 }}>
        <button className="ui-pill on">Photo try-on (2D)</button>
        <button className="ui-pill" disabled title="No AR pipeline exists in this build">
          Live AR (3D)
        </button>
      </div>

      {/* The occasion picker this screen was missing. One failing occasion no
          longer leaves a dead page. */}
      <div className="ui-pills" style={{ marginBottom: 20 }}>
        {OCCASIONS.slice(0, 8).map((o) => (
          <button
            key={o.id}
            className={`ui-pill${occasion === o.ask ? " on" : ""}`}
            disabled={busy}
            onClick={() => setOccasion(o.ask)}
          >
            {o.title}
          </button>
        ))}
      </div>

      <BodyPhotoPanel
        onChange={(n, tp) => {
          setConsented(n);
          setProvider(tp);
        }}
      />

      {busy ? <p className="ui-sub">Building looks…</p> : null}

      {!busy && outfits.length === 0 ? (
        <div className="ui-empty" style={{ textAlign: "left" }}>
          <b style={{ color: "var(--ink)" }}>No outfit for this occasion.</b>
          {note ? <p style={{ margin: "8px 0 0" }}>{note}</p> : null}

          {missing.length > 0 ? (
            <>
              <p style={{ margin: "14px 0 6px" }}>
                Your wardrobe has nothing catalogued in:
              </p>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {missing.map((m) => (
                  <li key={m.label}>
                    <b style={{ color: "var(--ink)" }}>{m.label}</b>
                    {m.why ? ` — ${m.why}` : ""}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p style={{ margin: "14px 0 0" }}>
              You do own the right kinds of garment, so this is a filter rather than a gap —
              usually laundry, the weather target, or the dress code for this occasion. Try
              another occasion above.
            </p>
          )}

          <p style={{ margin: "14px 0 0" }}>
            <Link href="/wardrobe" style={{ color: "var(--accent)" }}>
              Open your wardrobe →
            </Link>
            {wardrobe ? (
              <span className="ui-sub"> · {wardrobe.length} garments catalogued</span>
            ) : null}
          </p>
        </div>
      ) : null}

      {outfits.length > 0 ? (
        <div className="ui-grid">
          {outfits.map((o, i) => (
            <OutfitCard key={i} outfit={o} index={i} />
          ))}
        </div>
      ) : null}

      {/* REPORTS STATE, DOES NOT ASSUME IT.
          An earlier version hardcoded "you still need VTON_API_TOKEN". That is
          true for a ZeroGPU Space and FALSE for a self-hosted tunnel, where no
          auth exists and an empty token is correct — so the page told users a
          working feature was broken. Both facts below now come from the
          server: `provider` is the third party it names, and it only names one
          when a provider is configured. */}
      <div className="ui-unavailable" style={{ marginTop: 22 }}>
        {consented > 0 && provider ? (
          <>
            <b>Try-on is ready.</b> A body photo is stored with your consent, and renders go to{" "}
            {provider}. Tap Try On on any look — a render takes a few minutes, and the card shows
            the result when it is done.
          </>
        ) : consented > 0 ? (
          <>
            <b>Body photo: done.</b> No try-on provider is configured on this deployment, so every
            look degrades to a flat-lay board. That degrade is the designed behaviour, not a
            failure.
          </>
        ) : provider ? (
          <>
            <b>One thing left: a body photo.</b> Add one above with consent and renders will go to{" "}
            {provider}. Until then every look degrades to a flat-lay board.
          </>
        ) : (
          <>
            <b>Two things are needed before a render happens.</b> A body photo with explicit
            consent (above), and a try-on provider configured on the deployment. Until both are
            true every look degrades to a flat-lay board, which is the designed behaviour.
          </>
        )}{" "}
        <b>Live AR is not built at all.</b>
      </div>
    </Shell>
  );
}
