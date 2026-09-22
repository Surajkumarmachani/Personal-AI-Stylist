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

import { Suspense, useCallback, useEffect, useState } from "react";
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

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setRes(await askStylist(occasion, 9));
    } catch {
      setRes(null);
    } finally {
      setBusy(false);
    }
  }, [occasion]);

  useEffect(() => {
    if (email) void load();
  }, [email, load]);

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

      <div className="ui-pills" style={{ marginBottom: 20 }}>
        <button className="ui-pill on">All looks</button>
        <button className="ui-pill">From your wardrobe</button>
        <button className="ui-pill" disabled title="No product catalogue is connected">
          New picks
        </button>
        <button className="ui-pill" disabled title="No product catalogue is connected">
          Premium
        </button>
      </div>

      <div className="ui-unavailable" style={{ marginBottom: 20 }}>
        <b>New picks and Premium are off.</b> Both need a product catalogue and a merchant
        integration; this system only knows the clothes you have photographed. Every look below
        is built from your own wardrobe.
      </div>

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
