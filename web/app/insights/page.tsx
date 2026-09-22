"use client";

/** What the wardrobe is actually doing — worn, unworn, and what it costs.
 *
 * WHY THIS SCREEN EXISTS
 * Four endpoints answered these questions and NOTHING in the product called
 * them: `/wardrobe/most-worn` (with cost-per-wear), `/me/wear-through`,
 * `/me/style`. They were reachable only from the build console or not at all.
 *
 * Cost-per-wear is the number that makes a wardrobe app worth reopening —
 * it is the one thing the app knows that the user cannot work out in their
 * head — and it was computed, stored and never shown.
 *
 * NOTHING HERE IS INVENTED. Every figure is read from an endpoint; where the
 * data does not exist yet the screen says so rather than rendering a zero
 * that looks like a measurement.
 */

import { useEffect, useState } from "react";
import Shell from "../Shell";
import SignIn from "../SignIn";
import { restoreSession } from "../session";
import {
  mostWorn,
  styleSummary,
  wearThrough,
  type MostWorn,
  type StyleSummary,
  type WearThrough,
} from "@/lib/api";
import "../ui.css";

function money(minor: number | null, currency: string | null): string {
  if (minor == null) return "—";
  // Minor units: the API stores paise/cents because storing money as a float
  // is how rounding errors become support tickets.
  const major = minor / 100;
  return `${currency === "INR" ? "₹" : ""}${major.toFixed(major < 10 ? 2 : 0)}`;
}

export default function InsightsPage() {
  const [email, setEmail] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [worn, setWorn] = useState<MostWorn | null>(null);
  const [through, setThrough] = useState<WearThrough | null>(null);
  const [style, setStyle] = useState<StyleSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!email) return;
    mostWorn(12).then(setWorn).catch((e) => setErr(String(e)));
    wearThrough().then(setThrough).catch(() => undefined);
    styleSummary().then(setStyle).catch(() => undefined);
  }, [email]);

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  const rate = through?.wear_through_rate;

  return (
    <Shell email={email} back>
      <h1 style={{ margin: "0 0 20px", fontSize: 26, fontWeight: 640 }}>Insights</h1>

      <div className="ui-panel" style={{ marginBottom: 18 }}>
        <h2 className="ui-h3">Are you wearing what I suggest?</h2>
        {through ? (
          <>
            <p className="ui-sub" style={{ marginBottom: 6 }}>
              <b style={{ color: "var(--ink)", fontSize: 22 }}>
                {rate == null ? "—" : `${Math.round(rate * 100)}%`}
              </b>{" "}
              over the last {through.window_days} days
            </p>
            <p className="ui-sub">
              {through.worn_outfits} of {through.suggested_outfits} suggested outfits were
              logged as worn.{" "}
              {through.suggested_outfits === 0
                ? "Nothing has been suggested yet, so there is nothing to measure."
                : "Logging a wear from your wardrobe is what moves this."}
            </p>
          </>
        ) : (
          <p className="ui-sub">Loading…</p>
        )}
      </div>

      <div className="ui-panel" style={{ marginBottom: 18 }}>
        <h2 className="ui-h3">Most worn, and what each wear costs</h2>
        <p className="ui-sub" style={{ marginBottom: 12 }}>
          Cost per wear is the price divided by the number of times you have logged it — the
          case for keeping a thing, or for not buying another one like it.
        </p>
        {!worn ? (
          <p className="ui-sub">Loading…</p>
        ) : worn.items.length === 0 ? (
          <p className="ui-sub">
            Nothing has been logged as worn yet. Tap <b>Wore it</b> on a garment in your
            wardrobe and it will show up here.
          </p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                  <th style={{ padding: "6px 8px 6px 0" }}>Garment</th>
                  <th style={{ padding: "6px 8px" }}>Wears</th>
                  <th style={{ padding: "6px 8px" }}>Last worn</th>
                  <th style={{ padding: "6px 0 6px 8px" }}>Cost / wear</th>
                </tr>
              </thead>
              <tbody>
                {worn.items.map((it) => (
                  <tr key={it.id} style={{ borderTop: "1px solid var(--line)" }}>
                    <td style={{ padding: "8px 8px 8px 0" }}>
                      {(it.subcategory ?? "item").replace(/_/g, " ")}
                      <span className="ui-sub"> · {(it.primary_colour ?? "").replace(/_/g, " ")}</span>
                    </td>
                    <td style={{ padding: "8px" }}>{it.wears}</td>
                    <td style={{ padding: "8px" }} className="ui-sub">{it.last_worn ?? "—"}</td>
                    <td style={{ padding: "8px 0 8px 8px" }}>
                      {money(it.cost_per_wear_minor ?? null, it.currency ?? null)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="ui-panel">
        <h2 className="ui-h3">What I think your taste is</h2>
        {style ? (
          <>
            <p className="ui-sub">
              Built from <b style={{ color: "var(--ink)" }}>{style.events_applied}</b> reactions
              — {Object.entries(style.feedback_by_kind)
                .map(([k, n]) => `${n} ${k}`)
                .join(", ") || "none yet"}.
            </p>
            <p className="ui-sub" style={{ marginTop: 6 }}>
              {style.events_applied < 10 ? (
                <>
                  Below 10 reactions this is <b>not used</b> in ranking — it would be noise
                  rather than taste. {10 - style.events_applied} more to go.
                </>
              ) : (
                <>
                  It carries 20% of an outfit&apos;s score. A {style.dimensions}-dimension vector,
                  rebuilt from the event log, so nothing here is guesswork you cannot audit.
                </>
              )}
            </p>
          </>
        ) : (
          <p className="ui-sub">Loading…</p>
        )}
      </div>

      {err ? <p className="ui-err" style={{ marginTop: 12 }}>{err}</p> : null}
    </Shell>
  );
}
