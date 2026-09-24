"use client";

/** Home / Dashboard. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Shell from "./Shell";
import SignIn from "./SignIn";
import OutfitCard from "./OutfitCard";
import FillTheGap from "./FillTheGap";
import Onboarding from "./Onboarding";
import DressesAs from "./DressesAs";
import Image from "next/image";
import { OCCASIONS } from "./OCCASIONS";
import {
  askStylist,
  listGarments,
  todaysLook,
  unlogWear,
  wornToday,
  type ChatOutfit,
  type WornToday,
} from "@/lib/api";
import { restoreSession } from "./session";
import "./ui.css";

const QUICK = ["Wedding", "Party", "Office", "Date", "Travel", "Casual"];

export default function Home() {
  const [email, setEmail] = useState<string | null>(null);
  // `null` means BOTH "signed out" and "still checking", and showing
  // sign-in during the check flashes the form at every signed-in user on
  // every load. This separates the two.
  const [checking, setChecking] = useState(true);
  const [q, setQ] = useState("");
  const [outfits, setOutfits] = useState<ChatOutfit[]>([]);
  const [reply, setReply] = useState<string | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  // Why these outfits are on screen when nobody asked for them. Without it
  // the home page silently shows looks for an occasion the user never named,
  // which is the thing the suggestions endpoint used to do quietly and now
  // reports.
  const [why, setWhy] = useState<string | null>(null);
  // `null` = not asked yet, and that is NOT the same as "wore nothing".
  // Branching on the second before the first has answered would flash the
  // suggestions at someone who has already dressed.
  const [worn, setWorn] = useState<WornToday | null>(null);
  // Did the user ASK, or is this the unprompted daily suggestion?
  //
  // The "already dressed" gate below must apply only to the unprompted one.
  // Someone who wore a shirt this morning and then types "dinner date" into
  // the hero has asked a direct question, and swallowing the answer because
  // of what they wore earlier is the app refusing to respond.
  const [asked, setAsked] = useState(false);
  // The RESOLVED occasion of the last question, so the shop panel is keyed on
  // what the outfits were built for, not on the words typed. Explore does the
  // same, for the same reason: the two must not disagree about the occasion.
  const [askedOccasion, setAskedOccasion] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  /** Undo one wear, then re-read. Re-reading rather than splicing the item
   *  out locally: if that was the last garment, the whole section must give
   *  way to the suggestions, and only the server knows whether it was. */
  async function undoWear(garmentId: string) {
    if (!worn || removing) return;
    setRemoving(garmentId);
    try {
      await unlogWear(garmentId, worn.date);
      setWorn(await wornToday());
    } catch {
      /* leave the card in place: a failed delete must not look like a success */
    } finally {
      setRemoving(null);
    }
  }
  const router = useRouter();

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    if (email) listGarments().then((g) => setCount(g.length)).catch(() => setCount(null));
  }, [email]);

  // WHAT THEY ALREADY WORE, asked before anything is offered. Someone who has
  // dressed does not need to be sold an outfit.
  useEffect(() => {
    if (!email) return;
    let off = false;
    void (async () => {
      try {
        const w = await wornToday();
        if (!off) setWorn(w);
      } catch {
        // Treated as "wore nothing" rather than blocking the page: the
        // suggestions are the safe fallback, and an error here must not leave
        // the home screen empty.
        if (!off) setWorn({ date: "", items: [], wore_something: false });
      }
    })();
    return () => {
      off = true;
    };
  }, [email]);

  // TODAY'S LOOK, UNASKED. No occasion is sent, so the server resolves one
  // from the calendar when it can. This is the difference between an app that
  // waits to be asked and one that has already worked it out.
  useEffect(() => {
    if (!email) return;
    todaysLook(4)
      .then((r) => {
        if (!r.outfits?.length) return;
        setOutfits(r.outfits);
        const occasion = String(
          (r.context as { occasion?: string } | null)?.occasion ?? "",
        ).replace(/_/g, " ");
        if (r.occasion_source === "calendar") {
          const events =
            r.calendar_events_seen === 1 ? "1 event" : `${r.calendar_events_seen} events`;
          setWhy(
            `From your calendar — ${events} today read as ${occasion}. ` +
              (r.occasion_reason ?? ""),
          );
        } else {
          setWhy(
            `Nothing on your calendar to go on, so this is an everyday ${occasion} look. ` +
              `Connect your calendar in Profile and I will dress you for what is actually on.`,
          );
        }
      })
      .catch(() => undefined);
  }, [email]);

  const ask = useCallback(
    async (message: string) => {
      if (!message.trim() || busy) return;
      setBusy(true);
      try {
        const res = await askStylist(message, 4);
        setOutfits(res.outfits);
        setReply(res.reply);
        setAsked(true);
        setAskedOccasion(res.needs_clarification ? null : (res.understood?.occasion ?? null));
        // The user has now named an occasion, so the calendar provenance no
        // longer describes what is on screen.
        setWhy(null);
      } catch (e) {
        setReply(String(e));
        setOutfits([]);
        setAskedOccasion(null);
      } finally {
        setBusy(false);
      }
    },
    [busy],
  );

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  return (
    <Shell email={email}>
      {/* Above the hero: a new account has nothing to suggest, and a
          marketing headline over an empty grid is the least useful screen the
          product can show. */}
      <Onboarding />
      {/* Once, for accounts made before sign-up asked. Renders nothing after. */}
      <DressesAs variant="prompt" />

      <section className="ui-hero">
        <h1>
          Your Style
          <br />
          Your Story
        </h1>
        <p>AI-powered outfits for every version of you.</p>
        <form
          className="ui-ask"
          onSubmit={(e) => {
            e.preventDefault();
            void ask(q);
          }}
        >
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="What are you dressing for today?"
            aria-label="What are you dressing for today?"
            disabled={busy}
          />
          <button type="submit" disabled={busy || !q.trim()} aria-label="Ask">
            {busy ? "…" : "→"}
          </button>
        </form>
        <div className="ui-pills">
          {QUICK.map((x) => (
            <button
              key={x}
              className="ui-pill"
              disabled={busy}
              onClick={() => {
                setQ(x);
                void ask(x);
              }}
            >
              {x}
            </button>
          ))}
        </div>
      </section>

      <section>
        <div className="ui-head">
          <h2 className="ui-h2">Popular occasions</h2>
          <Link href="/occasions" style={{ color: "var(--accent)", fontSize: 13.5 }}>
            All occasions →
          </Link>
        </div>
        <div className="ui-grid tight">
          {OCCASIONS.slice(0, 6).map((o, i) => (
            <button
              key={o.id}
              className="ui-card"
              style={{ animationDelay: `${i * 40}ms`, textAlign: "left", padding: 0, border: "1px solid var(--line)" }}
              onClick={() => router.push(`/explore?o=${encodeURIComponent(o.ask)}`)}
            >
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
      </section>

      <p className="ui-sub" style={{ marginTop: 26 }}>
        {count === null ? "" : `${count} garments catalogued — every look is built from clothes you own.`}
      </p>
      {/* WHAT THEY WORE, when they wore something. The answer to "are we
          done here?" — and the reason the suggestions above are absent. */}
      {worn !== null && worn.wore_something ? (
        <section style={{ marginTop: 34 }}>
          <div className="ui-head">
            <h2 className="ui-h2">What you wore today</h2>
            <Link href="/wardrobe" style={{ color: "var(--accent)", fontSize: 13.5 }}>
              Wardrobe →
            </Link>
          </div>
          <div className="ui-grid tight">
            {worn.items.map((g, i) => (
              <article
                key={g.id}
                className="ui-card"
                style={{ animationDelay: `${i * 40}ms` }}
              >
                <div className="ui-frame one" style={{ aspectRatio: "1 / 1" }}>
                  {g.cutout_url ? (
                    // eslint-disable-next-line @next/next/no-img-element -- presigned MinIO URL, see wardrobe/page.tsx
                    <img src={g.cutout_url} alt={g.subcategory ?? "garment"} loading="lazy" />
                  ) : (
                    <span className="ui-ph">{g.subcategory ?? g.slot ?? "item"}</span>
                  )}
                </div>
                <div className="ui-cbody">
                  <span className="ui-name" style={{ fontSize: 13.5 }}>
                    {(g.subcategory ?? g.slot ?? "item").replace(/_/g, " ")}
                  </span>
                  {g.note ? <p className="ui-sub">{g.note}</p> : null}
                  {/* ONE CLICK TO UNDO. A wear logged by mistake used to be
                      permanent from the UI — the endpoint existed and nothing
                      called it. It is not a cosmetic entry either: wear
                      history drives cost-per-wear and repeat-avoidance, so a
                      wrong one skews suggestions until it is removed. */}
                  <button
                    className="ui-btn"
                    style={{ marginTop: 6, fontSize: 11.5, padding: "3px 8px" }}
                    disabled={removing === g.id}
                    onClick={() => void undoWear(g.id)}
                    title="I did not wear this"
                  >
                    {removing === g.id ? "removing…" : "Didn't wear this"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {/* SUGGESTIONS ONLY WHEN THEY HAVE NOT DRESSED YET.
          Offering outfits to someone who already told us what they wore is
          the product talking over the user. `worn` is null until the answer
          is known, which is deliberately NOT treated as "wore nothing" —
          otherwise this flashes on every load before disappearing.
          Three, not four: this is now a prompt rather than the main event. */}
      {(asked || (worn !== null && !worn.wore_something)) && outfits.length > 0 ? (
        <section style={{ marginBottom: 34 }}>
          <div className="ui-head">
            <h2 className="ui-h2">{why ? "Today's look" : "Curated for you"}</h2>
            <Link href="/explore" style={{ color: "var(--accent)", fontSize: 13.5 }}>
              See all →
            </Link>
          </div>
          {reply ? <p className="ui-sub" style={{ marginBottom: 14 }}>{reply}</p> : null}
          {/* WHAT THIS WAS DRESSED FOR. Shown only when the user did not ask —
              if they typed the occasion themselves, repeating it back is
              noise. The calendar case names the rule phrase that matched, not
              the event title: the calendar panel promises titles never reach
              this app, and quoting a diary entry here would break that in the
              one place the user would notice. */}
          {why ? (
            <div
              className="ui-sub"
              style={{
                marginBottom: 14,
                padding: "10px 12px",
                border: "1px solid var(--line)",
                borderRadius: 10,
              }}
            >
              {why}
            </div>
          ) : null}
          <div className="ui-grid">
            {outfits.slice(0, 4).map((o, i) => (
              <OutfitCard key={i} outfit={o} index={i} />
            ))}
          </div>
        </section>
      ) : null}

      {/* ASKED, AND THE WARDROBE CAME UP EMPTY. The reply says which piece is
          missing; it used to render nowhere, because the section above only
          exists when there are outfits — so the question got no answer at all. */}
      {asked && outfits.length === 0 && reply ? (
        <section style={{ marginBottom: 34 }}>
          <p className="ui-sub">{reply}</p>
        </section>
      ) : null}

      {/* Renders nothing unless the occasion's pool actually came back short. */}
      {asked && askedOccasion ? <FillTheGap occasion={askedOccasion} /> : null}
    </Shell>
  );
}
