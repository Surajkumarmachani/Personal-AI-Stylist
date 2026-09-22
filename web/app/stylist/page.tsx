"use client";

/** AI Stylist chat.
 *
 * "WHY THESE?" IS NOT COPY. The design shows three ticked reasons under each
 * answer; here they are read off the scorer's actual `score_breakdown` —
 * only the sub-scores that reported `informative: true`, phrased in English.
 * A sub-score that contributed nothing is not listed, so the panel cannot
 * claim a reason the ranking did not actually use.
 */

import { useEffect, useRef, useState } from "react";
import Shell from "../Shell";
import SignIn from "../SignIn";
import OutfitCard from "../OutfitCard";
import { askStylist, type ChatOutfit, type ChatReply } from "@/lib/api";
import { restoreSession } from "../session";
import "../ui.css";

type Bubble = { who: "me" | "bot" | "err"; text: string; why?: string[]; meta?: string };

const CHIPS = [
  "What should I wear for Diwali?",
  "I have a client meeting tomorrow",
  "Wedding reception this weekend",
  "It's freezing and I'm going to the office",
  "Something for the gym",
];

/** Turn the scorer's breakdown into the ticked reasons. */
function reasons(o: ChatOutfit | undefined): string[] {
  if (!o?.score_breakdown) return [];
  const subs = (o.score_breakdown as { sub_scores?: Record<string, Record<string, unknown>> })
    .sub_scores;
  if (!subs) return [];
  const say: Record<string, string> = {
    colour_harmony: "The colours work together",
    formality_coherence: "Matches the formality of the occasion",
    weather_fit: "Suited to the weather",
    style_affinity: "Close to the style you react well to",
    novelty: "Things you haven't worn recently",
    trend_alignment: "In line with what's being worn lately",
  };
  return Object.entries(subs)
    .filter(([, v]) => v && (v as { informative?: boolean }).informative)
    .map(([k]) => say[k])
    .filter(Boolean) as string[];
}

export default function StylistPage() {
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const [bubbles, setBubbles] = useState<Bubble[]>([
    {
      who: "bot",
      text: "Tell me the occasion — Diwali, a client meeting, a wedding reception — and I'll build looks from your wardrobe.",
    },
  ]);
  const [outfits, setOutfits] = useState<ChatOutfit[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  // BRACES ARE LOAD-BEARING. As a concise arrow body this was
  //
  //     useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), deps)
  //
  // which RETURNS whatever scrollIntoView returns, and React treats an
  // effect's return value as its cleanup function.
  //
  // For years that was safe: scrollIntoView returned undefined. Chrome 153
  // ships the scroll-completion proposal and it now returns a PROMISE
  // (verified in this browser: constructor.name === "Promise", typeof !==
  // "function"). So React stored a promise as the cleanup and, the next time
  // this effect re-ran, called it — `TypeError: i is not a function`, thrown
  // inside the commit phase, uncaught, which tears down the tree and leaves
  // Chrome's "This page couldn't load".
  //
  // The deps are why it presented as "the AI Stylist page crashes when I ask
  // it something": the cleanup only runs on the SECOND pass, and `bubbles`
  // first changes when a message is sent. Page load was always fine.
  //
  // A block body returns undefined, which is what React wants. Do not
  // "simplify" this back.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [bubbles, busy]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setDraft("");
    setBubbles((b) => [...b, { who: "me", text: message }]);
    setBusy(true);
    try {
      const res: ChatReply = await askStylist(message, 6);
      setOutfits(res.outfits);
      const bits = [res.ranking_source, res.served_from].filter(Boolean) as string[];
      setBubbles((b) => [
        ...b,
        {
          who: "bot",
          text: res.reply,
          why: reasons(res.outfits[0]),
          meta: bits.length ? bits.join(" · ") : undefined,
        },
      ]);
      if (res.needs_clarification && res.examples?.length) {
        setBubbles((b) => [...b, { who: "bot", text: `Try: ${res.examples!.join(", ")}` }]);
      }
    } catch (e) {
      setBubbles((b) => [...b, { who: "err", text: String(e) }]);
      setOutfits([]);
    } finally {
      setBusy(false);
    }
  }

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell
      email={email}
      back
      title={
        <div>
          <div style={{ fontSize: 16, fontWeight: 640 }}>AI Stylist</div>
          <div style={{ fontSize: 12, color: "var(--ok)" }}>● Online</div>
        </div>
      }
    >
      <div className="ui-chat">
        {bubbles.map((b, i) => (
          <div key={i} className={`ui-msg ${b.who}`}>
            <div className="bubble">
              {b.text}
              {b.why && b.why.length ? (
                <div className="ui-why">
                  <b>Why these?</b>
                  <ul>
                    {b.why.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {b.meta ? <small>{b.meta}</small> : null}
            </div>
          </div>
        ))}
        {busy ? (
          <div className="ui-msg">
            <div className="bubble">…</div>
          </div>
        ) : null}
        <div ref={endRef} />

        <div className="ui-pills" style={{ margin: "16px 0" }}>
          {CHIPS.map((c) => (
            <button key={c} className="ui-pill" onClick={() => void send(c)} disabled={busy}>
              {c}
            </button>
          ))}
        </div>

        <form
          className="ui-ask"
          style={{ maxWidth: "100%" }}
          onSubmit={(e) => {
            e.preventDefault();
            void send(draft);
          }}
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Ask me anything about what to wear…"
            aria-label="Ask the stylist"
            disabled={busy}
          />
          <button type="submit" disabled={busy || !draft.trim()} aria-label="Send">
            ➤
          </button>
        </form>
      </div>

      {outfits.length > 0 ? (
        <section style={{ marginTop: 30 }}>
          <h2 className="ui-h2">Looks</h2>
          <div className="ui-grid">
            {outfits.map((o, i) => (
              <OutfitCard key={i} outfit={o} index={i} />
            ))}
          </div>
        </section>
      ) : null}
    </Shell>
  );
}
