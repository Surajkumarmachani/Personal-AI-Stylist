"use client";

/** Ask for the user's city, once.
 *
 * WHY THIS SCREEN EXISTS
 * `weather_fit` carries 0.15 of the outfit score and `monsoon_suitability` is
 * a HARD filter — and both ran on a hard-coded 26C for every request this
 * system has ever served. The weather client was written in Phase 6 with
 * cache keys, 2dp privacy rounding and a degrade path, and had zero callers,
 * because nothing could answer "where?". This is that question.
 *
 * A CITY, NOT navigator.geolocation. The browser API is more precise and
 * worse here: it prompts before the user has been told why, returns precision
 * the backend deliberately discards (2dp, ~1.1km), and on desktop routinely
 * geolocates the ISP. A city is answerable, auditable and changeable.
 *
 * THE RESOLVED LABEL IS ALWAYS SHOWN, AND SO ARE THE ALTERNATIVES.
 * "Bangalore" resolves upstream ONLY to `Bangalore Town, Sindh, Pakistan` —
 * the Indian city is indexed as Bengaluru — so accepting the top hit silently
 * would set a user's weather to Sindh permanently with nothing on screen to
 * reveal it. Echoing "Patna, Bihar, India" back, plus the runners-up, makes a
 * wrong match visible in one glance.
 */

import { useEffect, useState } from "react";
import {
  clearLocation,
  getLocation,
  setLocation,
  type HomeLocation,
  type LocationChoice,
} from "@/lib/api";

export default function HomeCity({ onChanged }: { onChanged?: () => void }) {
  const [current, setCurrent] = useState<HomeLocation | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [choice, setChoice] = useState<LocationChoice | null>(null);

  useEffect(() => {
    void getLocation().then(setCurrent).catch(() => undefined);
  }, []);

  async function save(place: string, index = 0) {
    if (!place.trim() || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const got = await setLocation(place.trim(), index);
      setChoice(got);
      setCurrent(got);
      setDraft("");
      onChanged?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function forget() {
    setBusy(true);
    try {
      await clearLocation();
      setCurrent(null);
      setChoice(null);
      onChanged?.();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ui-panel">
      <h2 className="ui-h3">Your city</h2>
      <p className="ui-sub" style={{ marginBottom: 12 }}>
        Used only to look up the temperature, which decides how warmly an outfit is
        put together and whether rain-unfriendly fabrics are excluded. Stored to about
        1&nbsp;km — never your exact position — and you can clear it at any time.
      </p>

      {current?.place ? (
        <p className="ui-sub" style={{ marginBottom: 10 }}>
          Currently <b style={{ color: "var(--ink)" }}>{current.place}</b>
          {current.latitude != null ? ` (${current.latitude}, ${current.longitude})` : ""}
        </p>
      ) : (
        <p className="ui-sub" style={{ marginBottom: 10 }}>
          Not set — outfits are being built for a placeholder 26&nbsp;°C.
        </p>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save(draft);
          }}
          placeholder="Patna, Bengaluru, Mumbai…"
          aria-label="Your city"
          disabled={busy}
          style={{ flex: "1 1 200px", fontSize: 13 }}
        />
        <button className="ui-btn primary" onClick={() => void save(draft)} disabled={busy}>
          {busy ? "Looking up…" : "Save"}
        </button>
        {current?.place ? (
          <button className="ui-btn" onClick={() => void forget()} disabled={busy}>
            Clear
          </button>
        ) : null}
      </div>

      {/* The alternatives are the whole point — see the file comment. */}
      {choice && choice.alternatives.length > 0 ? (
        <div className="ui-sub" style={{ marginTop: 10 }}>
          Not the right one? Also matched:
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
            {choice.alternatives.map((a) => (
              <button
                key={a.index}
                className="ui-pill"
                disabled={busy}
                onClick={() => void save(choice.place ?? draft, a.index)}
              >
                {a.place}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}
    </div>
  );
}
