"use client";

/** One recommended outfit.
 *
 * The frame holds the user's OWN cutouts. The product's whole claim is
 * "clothes you already own", and stock imagery here would quietly break it —
 * which is also why no card carries a price or a Buy control: there is no
 * catalogue behind this system, only a wardrobe.
 */

import { useState } from "react";
import { saveOutfit, type ChatOutfit } from "@/lib/api";

/** A name for the look, DERIVED from its garments rather than invented.
 *
 * The design shows "Floral Elegance", "Modern Chic", "Royal Vibes". Those are
 * editorial copy with nothing behind them; repeating a fixed list would look
 * identical on screen and mean nothing. This reads the outfit's actual
 * subcategories and formality, so the label is true by construction and
 * changes when the outfit does.
 */
export function lookName(o: ChatOutfit): { name: string; tags: string[] } {
  const subs = o.garments.map((g) => (g.subcategory ?? "").toLowerCase());
  const ethnic = subs.some((s) =>
    /kurta|saree|lehenga|sherwani|salwar|anarkali|dupatta|choli|churidar|dhoti|bandhgala/.test(s),
  );
  const tags: string[] = [];
  let name: string;
  if (ethnic) {
    name = "Ethnic Edit";
    tags.push("Traditional");
  } else if (o.garments.length >= 5) {
    name = "Layered Look";
    tags.push("Layered");
  } else if (o.garments.length <= 3) {
    name = "Clean Lines";
    tags.push("Minimal");
  } else {
    name = "Everyday Classic";
    tags.push("Classic");
  }
  const colours = new Set(o.garments.map((g) => g.primary_colour).filter(Boolean));
  if (colours.size === 1) tags.push("Tonal");
  if (o.score >= 0.75) tags.push("Top pick");
  return { name, tags };
}

export default function OutfitCard({
  outfit,
  index,
  onOpen,
}: {
  outfit: ChatOutfit;
  index: number;
  onOpen?: (o: ChatOutfit) => void;
}) {
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const { name, tags } = lookName(outfit);

  async function save(e: React.MouseEvent) {
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      // `saved` is a real feedback kind, so the heart is not decorative — it
      // writes an event the Saved Looks screen reads back.
      await saveOutfit(outfit.garments.map((g) => g.id));
      setSaved(true);
    } catch {
      /* a failed save must not break the card it sits on */
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      className="ui-card"
      style={{ animationDelay: `${index * 50}ms`, cursor: onOpen ? "pointer" : undefined }}
      onClick={() => onOpen?.(outfit)}
    >
      <div className={`ui-frame${outfit.garments.length === 1 ? " one" : ""}`}>
        {outfit.garments.map((g) =>
          g.cutout_url ? (
            <img key={g.id} src={g.cutout_url} alt={g.subcategory ?? "garment"} loading="lazy" />
          ) : (
            <span key={g.id} className="ui-ph">{g.subcategory ?? g.slot ?? "item"}</span>
          ),
        )}
        <button
          className={`ui-heart${saved ? " on" : ""}`}
          onClick={save}
          disabled={busy}
          aria-label={saved ? "Saved" : "Save this look"}
        >
          {saved ? "♥" : "♡"}
        </button>
      </div>
      <div className="ui-cbody">
        <span className="ui-name">{name}</span>
        <p className="ui-sub">{outfit.garments.length} pieces you own</p>
        <div className="ui-tags">
          {tags.map((t) => (
            <span key={t} className="ui-tag">{t}</span>
          ))}
        </div>
      </div>
    </article>
  );
}
