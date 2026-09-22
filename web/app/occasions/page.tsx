"use client";

/** Occasion selection — the full taxonomy grid. */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Image from "next/image";
import Shell from "../Shell";
import SignIn from "../SignIn";
import { OCCASIONS } from "../OCCASIONS";
import CustomOccasions from "../CustomOccasions";
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
            {/* next/image, not <img>: these are local files under public/, which
                is the case the framework optimises — it serves a modern format,
                lazy-loads below the fold and reserves the box so the grid does
                not jump as twelve photos arrive. `alt=""` because the title
                directly beneath says the same thing; announcing it twice is
                noise to a screen reader. */}
            <div
              className="ui-frame photo"
              style={{ aspectRatio: "1 / 1" }}
            >
              <Image
                src={o.img}
                alt=""
                width={520}
                height={520}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            </div>
            <div className="ui-cbody">
              <span className="ui-name">{o.title}</span>
              <p className="ui-sub">{o.sub}</p>
            </div>
          </button>
        ))}
      </div>
      <CustomOccasions />

      <div className="ui-unavailable" style={{ marginTop: 20 }}>
        <b>A custom occasion is a name, not a new category.</b> Every one resolves to a
        built-in occasion so the scorer has a formality and dress-code target to aim at —
        without that it would have nothing to rank against and would return no outfits.
      </div>
    </Shell>
  );
}
