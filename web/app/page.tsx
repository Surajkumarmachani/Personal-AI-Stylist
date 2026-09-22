"use client";

/** Home / Dashboard. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Shell from "./Shell";
import SignIn from "./SignIn";
import OutfitCard from "./OutfitCard";
import { OCCASIONS } from "./OCCASIONS";
import { askStylist, listGarments, type ChatOutfit } from "@/lib/api";
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
  const router = useRouter();

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    if (email) listGarments().then((g) => setCount(g.length)).catch(() => setCount(null));
  }, [email]);

  const ask = useCallback(
    async (message: string) => {
      if (!message.trim() || busy) return;
      setBusy(true);
      try {
        const res = await askStylist(message, 4);
        setOutfits(res.outfits);
        setReply(res.reply);
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
            <h2 className="ui-h2">Curated for you</h2>
            <Link href="/explore" style={{ color: "var(--accent)", fontSize: 13.5 }}>
              See all →
            </Link>
          </div>
          {reply ? <p className="ui-sub" style={{ marginBottom: 14 }}>{reply}</p> : null}
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
              <div className="ui-frame" style={{ aspectRatio: "1 / 1" }}>
                <span style={{ fontSize: 26, opacity: 0.35 }} aria-hidden="true">◇</span>
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
