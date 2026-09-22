"use client";

/** Home / Dashboard. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Shell from "./Shell";
import SignIn from "./SignIn";
import OutfitCard from "./OutfitCard";
import Onboarding from "./Onboarding";
import Image from "next/image";
import { OCCASIONS } from "./OCCASIONS";
import { askStylist, listGarments, todaysLook, type ChatOutfit } from "@/lib/api";
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
  const router = useRouter();

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    if (email) listGarments().then((g) => setCount(g.length)).catch(() => setCount(null));
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
        // The user has now named an occasion, so the calendar provenance no
        // longer describes what is on screen.
        setWhy(null);
      } catch (e) {
        setReply(String(e));
        setOutfits([]);
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

      {outfits.length > 0 ? (
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
            {outfits.map((o, i) => (
              <OutfitCard key={i} outfit={o} index={i} />
            ))}
          </div>
        </section>
      ) : reply ? (
        <div className="ui-empty" style={{ marginBottom: 34 }}>{reply}</div>
      ) : null}

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
    </Shell>
  );
}
