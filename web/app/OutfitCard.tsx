"use client";

/** One recommended outfit.
 *
 * The frame holds the user's OWN cutouts. The product's whole claim is
 * "clothes you already own", and stock imagery here would quietly break it —
 * which is also why no card carries a price or a Buy control: there is no
 * catalogue behind this system, only a wardrobe.
 */

import { useState } from "react";
import { requestTryOn, saveOutfit, type ChatOutfit } from "@/lib/api";

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

/** "1st", "2nd", "3rd", "4th"… English ordinals, including the teens.
 *
 * Written out rather than `n + "th"`, which produces "1th" and "23th". A
 * ranking label with a grammatical error in it reads as a bug in the ranking.
 */
export function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
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
  // Read off the outfit rather than passed in: every screen rendering a card
  // would otherwise have to thread it through, and one forgetting to is a
  // dead Try On button that looks identical to a working one.
  const hash = outfit.garment_set_hash;
  const [saved, setSaved] = useState(false);
  const [state, setState] = useState<string | null>(null);
  // The rendered try-on, once there is one. SHOWING it is the whole point:
  // an earlier version reported "rendered — reload to see it" and the card
  // still drew cutouts, so reloading changed nothing and the render was
  // invisible. A status line is not a feature.
  const [tryonUrl, setTryonUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { name, tags } = lookName(outfit);

  // The server states the rank. Falling back to `index + 1` keeps older
  // callers working, but the server's value wins wherever it is present —
  // a screen that filters its list would otherwise relabel the ranking.
  const rank = outfit.rank ?? index + 1;
  // The bandit is allowed to promote a worse-predicted outfit; that is
  // exploration, not a mistake. Saying "1st choice" about it would borrow
  // the scorer's endorsement, so the card says why it is here instead.
  const predicted = outfit.predicted_rank ?? rank;
  const promoted = predicted > rank;

  async function tryOn(e: React.MouseEvent) {
    e.stopPropagation();
    if (busy) return;
    if (!hash) {
      setState("this look has no id yet — reload and try again");
      return;
    }
    setBusy(true);
    setState("requesting…");
    try {
      const res = await requestTryOn(hash);
      // ALWAYS 200 by design. `rendered: false` carries a reason and a board;
      // `queued` means the worker has it and a render takes a few minutes.
      if (res.rendered && res.tryon_url) {
        setTryonUrl(res.tryon_url);
        setState("rendered");
      } else if (res.queued) {
        setState("queued — a render takes a few minutes. Tap again to check.");
      } else {
        setState(res.reason ?? "not available");
      }
    } catch (err) {
      setState(String(err));
    } finally {
      setBusy(false);
    }
  }

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
      <div className={`ui-frame${tryonUrl || outfit.garments.length === 1 ? " one" : ""}`}>
        {tryonUrl ? (
          // The render replaces the cutouts — it IS the answer to "what would
          // this look like on me", and showing both would bury it.
          <img src={tryonUrl} alt="You wearing this outfit" />
        ) : (
          outfit.garments.map((g) =>
          g.cutout_url ? (
            <img key={g.id} src={g.cutout_url} alt={g.subcategory ?? "garment"} loading="lazy" />
          ) : (
            <span key={g.id} className="ui-ph">{g.subcategory ?? g.slot ?? "item"}</span>
          ),
          )
        )}
        <span className="ui-rank" data-top={rank === 1 ? "1" : undefined} title={
          promoted
            ? `The scorer ranked this ${ordinal(predicted)}. Shown higher to vary what you see.`
            : `${ordinal(rank)} of what I'd recommend for this occasion`
        }>
          {rank}
        </span>
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
        <p className="ui-sub">
          <b style={{ color: "var(--ink)" }}>
            {rank === 1 ? "1st choice" : `${ordinal(rank)} choice`}
          </b>
          {" · "}
          {outfit.garments.length} pieces you own
        </p>
        {promoted ? (
          <p className="ui-sub" style={{ fontSize: 11.5 }}>
            Ranked {ordinal(predicted)} by score — shown higher to vary what you see.
          </p>
        ) : null}
        <div className="ui-tags">
          {tags.map((t) => (
            <span key={t} className="ui-tag">{t}</span>
          ))}
        </div>
        <button
          className="ui-btn"
          style={{ marginTop: 10 }}
          onClick={tryonUrl ? (e) => { e.stopPropagation(); setTryonUrl(null); setState(null); } : tryOn}
          disabled={busy}
        >
          {busy ? "…" : tryonUrl ? "Show garments" : "Try On"}
        </button>
        {state ? <p className="ui-sub">{state}</p> : null}
      </div>
    </article>
  );
}
