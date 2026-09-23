"use client";

/** One recommended outfit.
 *
 * The frame holds the user's OWN cutouts. The product's whole claim is
 * "clothes you already own", and stock imagery here would quietly break it —
 * which is also why no card carries a price or a Buy control: there is no
 * catalogue behind this system, only a wardrobe.
 */

import { useEffect, useRef, useState } from "react";
import {
  markOutfitWorn,
  outfitBoard,
  rateOutfit,
  requestTryOn,
  saveOutfit,
  type ChatOutfit,
} from "@/lib/api";

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
  // The verdict given on this card, if any. Local only: the point is to stop
  // the user rating the same outfit twice in a row and to show it registered.
  const [rated, setRated] = useState<"like" | "dislike" | null>(null);
  const [worn, setWorn] = useState(false);
  const [state, setState] = useState<string | null>(null);
  // The rendered try-on, once there is one. SHOWING it is the whole point:
  // an earlier version reported "rendered — reload to see it" and the card
  // still drew cutouts, so reloading changed nothing and the render was
  // invisible. A status line is not a feature.
  const [tryonUrl, setTryonUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Seconds since this card's render was enqueued. Drives the live counter,
  // so a three-minute wait LOOKS like progress instead of a frozen label.
  const [elapsed, setElapsed] = useState<number | null>(null);
  // Survives re-renders without causing them; cleared on unmount so a card
  // scrolled out of a list stops polling.
  const timers = useRef<{ poll?: ReturnType<typeof setInterval>; tick?: ReturnType<typeof setInterval> }>({});

  useEffect(
    () => () => {
      if (timers.current.poll) clearInterval(timers.current.poll);
      if (timers.current.tick) clearInterval(timers.current.tick);
    },
    [],
  );
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

  /** Watch a render to completion, showing elapsed time while it works. */
  function startPolling() {
    if (!hash) return;
    if (timers.current.poll) clearInterval(timers.current.poll);
    if (timers.current.tick) clearInterval(timers.current.tick);

    const startedAt = Date.now();
    setElapsed(0);
    setState("rendering");
    // Two timers on purpose: the counter ticks every second so the card feels
    // alive, while the network poll runs every four so a three-minute render
    // costs ~45 requests rather than ~180.
    timers.current.tick = setInterval(
      () => setElapsed(Math.round((Date.now() - startedAt) / 1000)),
      1000,
    );
    timers.current.poll = setInterval(async () => {
      // A render is 30-120s typically; give up well past the worst case
      // rather than polling a dead job forever.
      if (Date.now() - startedAt > 12 * 60 * 1000) {
        stopPolling();
        setState("still rendering after 12 minutes — tap Try On to check again");
        return;
      }
      try {
        const r = await requestTryOn(hash);
        if (r.rendered && r.tryon_url) {
          stopPolling();
          setTryonUrl(r.tryon_url);
          announce(r.skipped_slots ?? []);
        } else if (!r.queued) {
          // A failure, a quota refusal, or consent withdrawn mid-render: all
          // carry a reason, and none of them get better by waiting.
          stopPolling();
          setState(r.reason ?? "not available");
        }
      } catch {
        // A dropped request mid-poll is not the render failing. Keep waiting;
        // the timeout above is the backstop.
      }
    }, 4000);
  }

  function stopPolling() {
    if (timers.current.poll) clearInterval(timers.current.poll);
    if (timers.current.tick) clearInterval(timers.current.tick);
    timers.current = {};
    setElapsed(null);
    setBusy(false);
  }

  /** What a finished render is, and is not.
   *
   *  This line has been wrong in BOTH directions and the history is worth
   *  keeping. It first claimed untried garments were "your own from the
   *  photo" — false, the provider invents them. It was then corrected to say
   *  the face was an approximation too, which was true at the time. Face
   *  restoration landed afterwards and this string was not updated, so it
   *  went on calling the user's own composited face a guess.
   *
   *  What is actually true now: the head comes from the uploaded photo
   *  pixel-for-pixel; the garments named in the outfit are fitted by the
   *  provider; everything else in the frame — background, shoes, hands — is
   *  regenerated and invented. */
  function announce(skipped: string[]) {
    const missed = skipped.map((x) => x.replace(/_/g, " "));
    setState(
      missed.length
        ? `rendered — your face is your own photo. ${missed.join(" and ")} not tried on, so those and the background are invented by the model.`
        : "rendered — your face is your own photo. The background is regenerated by the model.",
    );
  }

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
        announce(res.skipped_slots ?? []);
      } else if (res.queued) {
        // POLL, rather than telling the user to keep tapping.
        //
        // Tapping was not just tedious, it was EXPENSIVE: before this change
        // every tap re-enqueued a duplicate job and spent one of ten daily
        // renders. The server now recognises a render already in flight and
        // neither re-enqueues nor charges for the question, which is what
        // makes polling on a timer safe to do at all.
        startPolling();
      } else {
        setState(res.reason ?? "not available");
      }
    } catch (err) {
      setState(String(err));
    } finally {
      setBusy(false);
    }
  }

  /** The flat-lay board: the outfit composited into one image.
   *
   * `outfitBoard` existed and NOTHING called it. It is also what try-on
   * degrades TO — the endpoint returns a board whenever a render is
   * unavailable — so the fallback was reachable only as a side effect of a
   * try-on that failed, never as a thing you could ask for.
   *
   * Worth asking for on its own: a board is pixel-accurate to clothes the
   * user owns, where a render is a guess about how they would look.
   */
  async function board(e: React.MouseEvent) {
    e.stopPropagation();
    if (busy || !hash) return;
    setBusy(true);
    setState("building the board…");
    try {
      const res = await outfitBoard(hash);
      if (res.board_url) {
        setTryonUrl(res.board_url);
        setState("flat-lay");
      } else {
        setState("no board yet — it is built with the nightly precompute");
      }
    } catch (e2) {
      setState(String(e2));
    } finally {
      setBusy(false);
    }
  }

  /** A VERDICT on this suggestion.
   *
   *  The recommender had no way to receive one. `saved` was the only kind the
   *  UI could send, and `apply_feedback` treats saving as intent rather than
   *  evidence, so it moves the style vector at half weight and the Thompson
   *  posteriors not at all. `bandit_arm` was consequently empty across the
   *  entire database — the loop was complete and unreachable.
   */
  async function rate(e: React.MouseEvent, kind: "like" | "dislike") {
    e.stopPropagation();
    if (busy || rated) return;
    setBusy(true);
    try {
      const r = await rateOutfit(outfit.garments.map((g) => g.id), kind);
      setRated(kind);
      // SHOW THAT IT LANDED, AND NAME WHAT MOVED. "Thanks for your feedback"
      // is what products say when nothing happened. The server returns which
      // Thompson arm changed, so this is the server's claim, not the
      // client's guess — and when no arm moved it does not pretend one did.
      setState(
        r.bandit_arm
          ? `${kind === "like" ? "liked" : "disliked"} — learned for ${r.bandit_arm.replace(/_/g, " ")} looks`
          : `${kind === "like" ? "liked" : "disliked"} — taste updated`,
      );
    } catch (err) {
      setState(String(err));
    } finally {
      setBusy(false);
    }
  }

  /** Wearing it is the real verdict. See `markOutfitWorn` for why this both
   *  teaches the recommender and is the only thing that makes the
   *  wear-through metric computable. */
  async function wore(e: React.MouseEvent) {
    e.stopPropagation();
    if (busy || worn) return;
    setBusy(true);
    try {
      const r = await markOutfitWorn(outfit.garments.map((g) => g.id));
      setWorn(true);
      setState(
        r.bandit_arm
          ? `logged as worn — learned for ${r.bandit_arm.replace(/_/g, " ")} looks`
          : "logged as worn",
      );
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
        {/* `<img>`, deliberately, not `next/image`. Every src below is a
            PRESIGNED MinIO URL: it carries an expiring signature and is a
            different string on every request. The image optimizer keys its
            cache on that URL, so it would never once hit — it would re-fetch
            and re-encode on each render — and whatever it did cache would
            outlive the signature it was fetched with. The optimizer is the
            wrong tool for short-lived signed URLs, so the rule is suppressed
            per-tag below rather than the URLs being reshaped to suit it. */}
        {tryonUrl ? (
          // The render replaces the cutouts — it IS the answer to "what would
          // this look like on me", and showing both would bury it.
          // eslint-disable-next-line @next/next/no-img-element -- presigned URL; see above
          <img src={tryonUrl} alt="You wearing this outfit" />
        ) : (
          outfit.garments.map((g) =>
          g.cutout_url ? (
            // eslint-disable-next-line @next/next/no-img-element -- presigned URL; see above
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
        {!tryonUrl ? (
          <button
            className="ui-btn"
            style={{ marginTop: 6, fontSize: 11.5, padding: "4px 9px" }}
            onClick={board}
            disabled={busy || !hash}
            title="A flat-lay of this outfit — your actual garments, composited"
          >
            Flat-lay
          </button>
        ) : null}
        {/* THE VERDICT BUTTONS. Without these the recommender cannot learn:
            the heart writes `saved`, which is intent rather than evidence and
            moves no Thompson posterior. */}
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <button
            className={`ui-btn${rated === "like" ? " primary" : ""}`}
            style={{ fontSize: 11.5, padding: "4px 9px" }}
            onClick={(e) => void rate(e, "like")}
            disabled={busy || rated !== null}
            title="Suggest more like this"
          >
            {rated === "like" ? "👍 liked" : "👍 More like this"}
          </button>
          <button
            className={`ui-btn${rated === "dislike" ? " primary" : ""}`}
            style={{ fontSize: 11.5, padding: "4px 9px" }}
            onClick={(e) => void rate(e, "dislike")}
            disabled={busy || rated !== null}
            title="Suggest fewer like this"
          >
            {rated === "dislike" ? "👎 noted" : "👎 Not for me"}
          </button>
          <button
            className={`ui-btn${worn ? " primary" : ""}`}
            style={{ fontSize: 11.5, padding: "4px 9px" }}
            onClick={(e) => void wore(e)}
            disabled={busy || worn}
            title="Counts as the strongest positive signal, and is what makes the wear-through metric measurable"
          >
            {worn ? "✓ worn" : "I wore this"}
          </button>
        </div>
        {elapsed !== null ? (
          /* A COUNTER, not a static label. The render genuinely takes
             minutes; the old text said "tap again to check" and looked
             identical whether the job was working, finished or long dead. */
          <p className="ui-sub" aria-live="polite">
            rendering… {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}
            {" — this usually takes 1–3 minutes"}
          </p>
        ) : state ? (
          <p className="ui-sub">{state}</p>
        ) : null}
      </div>
    </article>
  );
}
