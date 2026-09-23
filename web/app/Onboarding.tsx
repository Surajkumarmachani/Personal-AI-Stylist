"use client";

/** What a brand-new account sees instead of an empty grid.
 *
 * THE FIRST RUN WAS A DEAD END. A new user landed on a home page with no
 * outfits, a wardrobe with no garments, and no indication of what to do or
 * why nothing worked. Every capability in the product depends on there being
 * clothes catalogued, and nothing said so.
 *
 * THE STEPS ARE THE REAL DEPENDENCIES, NOT A TOUR.
 * Each one is checked against actual state, and each is here because the
 * product genuinely cannot work without it:
 *
 *   garments   nothing can be suggested from an empty wardrobe
 *   footwear   `feet` is the one slot EVERY outfit structure requires, so a
 *              wardrobe without shoes produces zero outfits no matter how
 *              much else is in it — the single most common reason a new user
 *              sees nothing
 *   city       `weather_fit` is 15% of the score and the wet-fabric filter is
 *              a hard exclusion; without a city both run on a placeholder
 *
 * Dismissable, and it disappears on its own once the wardrobe can dress
 * someone. A checklist that outlives its usefulness is nagging.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { getLocation, listGarments, type Garment } from "@/lib/api";

const DISMISSED_KEY = "stylist.onboarding.dismissed";

export default function Onboarding() {
  const [items, setItems] = useState<Garment[] | null>(null);
  const [hasCity, setHasCity] = useState<boolean | null>(null);
  const [dismissed, setDismissed] = useState(true);

  useEffect(() => {
    let off = false;
    void (async () => {
      const [garments, location] = await Promise.allSettled([listGarments(), getLocation()]);
      if (off) return;
      // Read HERE and not in a lazy `useState` initialiser: this component is
      // prerendered on the server, where `localStorage` does not exist, and
      // seeding state from it would hydrate to a different value than the
      // server sent. Deferring costs nothing, because the component renders
      // null until both requests above have answered anyway.
      try {
        setDismissed(localStorage.getItem(DISMISSED_KEY) === "1");
      } catch {
        setDismissed(false);
      }
      setItems(garments.status === "fulfilled" ? garments.value : []);
      setHasCity(
        location.status === "fulfilled" ? Boolean(location.value.weather_is_real) : false,
      );
    })();
    return () => {
      off = true;
    };
  }, []);

  if (items === null || hasCity === null) return null;

  const hasGarments = items.length > 0;
  // `feet` is required by every base structure, so this is not a nicety.
  const hasShoes = items.some((g) => g.slot === "feet");
  const done = hasGarments && hasShoes && hasCity;

  // Gone once it is no longer true, without anyone having to dismiss it.
  if (done || dismissed) return null;

  function hide() {
    setDismissed(true);
    try {
      localStorage.setItem(DISMISSED_KEY, "1");
    } catch {
      /* private mode: it just reappears next load, which is not harmful */
    }
  }

  const steps: { done: boolean; title: string; body: string; href: string; cta: string }[] = [
    {
      done: hasGarments,
      title: "Add some clothes",
      body: "Photograph them flat or on a hanger. One garment per photo works best, but a flat-lay of several is split automatically.",
      href: "/wardrobe",
      cta: "Open the wardrobe",
    },
    {
      done: hasShoes,
      title: "Add at least one pair of shoes",
      body: "Footwear is the one slot every outfit needs. Without a pair catalogued, no outfit can be built at all — however much else you add.",
      href: "/wardrobe",
      cta: "Add footwear",
    },
    {
      done: hasCity,
      title: "Tell me your city",
      body: "Used only for the temperature, which decides how warmly an outfit is put together and whether rain-unfriendly fabrics are excluded.",
      href: "/profile",
      cta: "Set your city",
    },
  ];

  const complete = steps.filter((s) => s.done).length;

  return (
    <div className="ui-panel" style={{ marginBottom: 22 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <h2 className="ui-h3">Let&apos;s get you dressed</h2>
        <button
          className="ui-btn"
          style={{ fontSize: 11.5, padding: "3px 8px" }}
          onClick={hide}
        >
          Hide
        </button>
      </div>
      <p className="ui-sub" style={{ marginBottom: 14 }}>
        {complete} of {steps.length} done. This disappears on its own once your wardrobe can
        dress you.
      </p>

      {steps.map((s) => (
        <div
          key={s.title}
          style={{
            display: "flex",
            gap: 10,
            alignItems: "flex-start",
            padding: "10px 0",
            borderTop: "1px solid var(--line)",
            opacity: s.done ? 0.55 : 1,
          }}
        >
          <span aria-hidden="true" style={{ fontSize: 15, lineHeight: 1.4 }}>
            {s.done ? "✓" : "○"}
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <b style={{ fontSize: 13.5 }}>{s.title}</b>
            <p className="ui-sub" style={{ margin: "2px 0 0" }}>{s.body}</p>
          </div>
          {!s.done ? (
            <Link href={s.href} className="ui-btn" style={{ fontSize: 11.5, padding: "4px 9px" }}>
              {s.cta}
            </Link>
          ) : null}
        </div>
      ))}
    </div>
  );
}
