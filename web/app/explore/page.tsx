"use client";

/** Outfit recommendations for one occasion.
 *
 * FILTER TABS: the design shows All Looks / From Your Wardrobe / New Picks /
 * Premium. Only the first two can exist here — every outfit this system
 * produces IS from your wardrobe, because there is no product catalogue and no
 * merchant feed behind "New Picks" or "Premium". Those two are rendered
 * disabled with the reason, rather than as tabs that quietly show the same
 * results and imply a catalogue that is not there.
 */

import { Suspense, useEffect, useState } from "react";
import FillTheGap from "../FillTheGap";
import { useSearchParams } from "next/navigation";
import Shell from "../Shell";
import SignIn from "../SignIn";
import OutfitCard from "../OutfitCard";
import Link from "next/link";
import { OCCASIONS } from "../OCCASIONS";
import { askStylist, type ChatOutfit, type ChatReply } from "@/lib/api";
import { restoreSession } from "../session";
import "../ui.css";

function ExploreInner() {
  const params = useSearchParams();
  // Seeded from the URL, then switchable in-page. Read-only from the URL left
  // one failing occasion as a dead end with no way out.
  const [occasion, setOccasion] = useState(params.get("o") ?? "casual");
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const [res, setRes] = useState<ChatReply | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);

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
      try {
        const r = await askStylist(occasion, 9);
        if (!off) setRes(r);
      } catch {
        if (!off) setRes(null);
      } finally {
        if (!off) setBusy(false);
      }
    })();
    return () => {
      off = true;
    };
  }, [email, occasion]);

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  const outfits: ChatOutfit[] = res?.outfits ?? [];
  const label = res?.understood?.occasion?.replace(/_/g, " ") ?? occasion;

  return (
    <Shell
      email={email}
      back
      title={
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, color: "var(--muted)" }}>Outfits for</div>
          <div style={{ fontSize: 18, fontWeight: 640, textTransform: "capitalize" }}>{label}</div>
        </div>
      }
    >
      <div className="ui-pills" style={{ marginBottom: 14 }}>
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

      {/* FOUR DEAD PILLS USED TO SIT HERE: "All looks", "From your wardrobe",
          "New picks" and "Premium".

          The last two were placeholders for a shop front, and what was
          actually built is the opposite of one: suggestions appear ONLY where
          this wardrobe cannot dress the occasion, below the looks, named by
          what is missing.

          The first two were worse, because they LOOKED live -- "All looks"
          even rendered as selected -- while neither carried an onClick. They
          also described a distinction this app does not have: every look here
          is built from clothes you own, so "all looks" and "from your
          wardrobe" are two names for the same set. The occasion pills above
          are the real filter. A control that cannot answer the question it
          poses is worse than no control. */}

      {busy ? <p className="ui-sub">Building looks…</p> : null}

      {outfits.length > 0 ? (
        <div className="ui-grid">
          {outfits.map((o, i) => (
            <OutfitCard key={i} outfit={o} index={i} />
          ))}
        </div>
      ) : !busy ? (
        <div className="ui-empty" style={{ textAlign: "left" }}>
          <b style={{ color: "var(--ink)" }}>No outfit for this occasion.</b>
          <p style={{ margin: "8px 0 0" }}>{res?.notes?.[0] ?? res?.reply ?? "Nothing matched."}</p>
          <p style={{ margin: "12px 0 0" }}>
            Try another occasion above, or{" "}
            <Link href="/wardrobe" style={{ color: "var(--accent)" }}>
              check your wardrobe
            </Link>
            .
          </p>
        </div>
      ) : null}

      {/* BELOW the looks, and only when the wardrobe actually came up short.
          Keyed on the RESOLVED taxonomy occasion from the reply, not the
          phrase in the input — the gap has to be for the same occasion the
          outfits above were built for, or the two contradict each other. */}
      {res?.understood?.occasion ? (
        <FillTheGap occasion={res.understood.occasion} />
      ) : null}
    </Shell>
  );
}

export default function ExplorePage() {
  // useSearchParams needs a Suspense boundary for static prerendering.
  return (
    <Suspense fallback={null}>
      <ExploreInner />
    </Suspense>
  );
}
