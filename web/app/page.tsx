"use client";

/**
 * Phase 2 wardrobe grid.
 *
 * One screen, three jobs: sign in, upload a flat-lay, watch it become a cutout.
 * The state badge per item is the visible half of the ingest state machine —
 * "processing / ready / needs review" maps onto job states, so a user is never
 * looking at a blank tile wondering whether anything is happening.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Garment,
  type JobEvent,
  ingest,
  listGarments,
  login,
  presign,
  register,
  setToken,
  streamJob,
  uploadToStorage,
} from "@/lib/api";

// jobs.state -> what the user is told.
function badgeFor(state: string): { cls: string; label: string } {
  switch (state) {
    case "complete":
    case "matted":
      return { cls: "ready", label: "ready" };
    case "needs_review":
      return { cls: "review", label: "needs review" };
    case "rejected":
    case "quarantined":
      return { cls: "review", label: "rejected" };
    default:
      // received / validated / sanitised / moderated — all in flight.
      return { cls: "processing", label: "processing" };
  }
}

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [signedIn, setSignedIn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [garments, setGarments] = useState<Garment[]>([]);
  const [progress, setProgress] = useState<Record<string, JobEvent>>({});
  const fileInput = useRef<HTMLInputElement>(null);
  const cleanups = useRef<Array<() => void>>([]);

  const refresh = useCallback(async () => {
    try {
      setGarments(await listGarments());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  // Abort any open SSE stream on unmount, or the connections (and their DB
  // sessions) leak until something else times them out.
  useEffect(() => () => cleanups.current.forEach((fn) => fn()), []);

  async function auth(mode: "register" | "login") {
    setBusy(true);
    setError(null);
    try {
      const fn = mode === "register" ? register : login;
      const { access_token } = await fn(email, password);
      setToken(access_token);
      setSignedIn(true);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function upload(files: FileList) {
    setBusy(true);
    setError(null);
    try {
      const uploaded: { upload_id: string; key: string }[] = [];
      for (const file of Array.from(files)) {
        const p = await presign(file.type || "image/jpeg");
        await uploadToStorage(p, file);
        uploaded.push({ upload_id: p.upload_id, key: p.key });
      }

      // One idempotency key per user action. A retry of the same click must
      // return the original jobs rather than starting a second pipeline.
      const idem = crypto.randomUUID();
      const { job_ids } = await ingest(uploaded, idem);

      await refresh();

      for (const jobId of job_ids) {
        const stop = streamJob(
          jobId,
          (e) => setProgress((prev) => ({ ...prev, [e.job_id]: e })),
          () => void refresh(),
        );
        cleanups.current.push(stop);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  const inFlight = Object.values(progress).filter(
    (p) => !["complete", "matted", "rejected", "needs_review", "quarantined"].includes(p.state),
  );

  return (
    <main>
      <h1>Wardrobe</h1>
      <p className="sub">
        Phase 2 — upload a flat-lay, get a background-removed cutout. No tagging yet.
      </p>

      {!signedIn ? (
        <div className="panel">
          <div className="row">
            <div>
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
            </div>
            <div>
              <label htmlFor="password">Password (12+ characters)</label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div style={{ flex: "0 0 auto", display: "flex", gap: 8 }}>
              <button onClick={() => void auth("register")} disabled={busy}>
                Register
              </button>
              <button className="ghost" onClick={() => void auth("login")} disabled={busy}>
                Sign in
              </button>
            </div>
          </div>
          {error && <div className="err">{error}</div>}
        </div>
      ) : (
        <div className="panel">
          <label htmlFor="photos">Add photos</label>
          <input
            id="photos"
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/heic,image/heif"
            multiple
            disabled={busy}
            onChange={(e) => e.target.files && void upload(e.target.files)}
          />
          <div className="progress">
            Flat-lay on a plain background works best — segmentation of
            multi-garment photos arrives in Phase 3.
          </div>
          {inFlight.length > 0 && (
            <div className="progress" style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <span className="spinner" />
              {inFlight.map((p) => (
                <span key={p.job_id}>
                  <code>{p.state}</code>
                </span>
              ))}
            </div>
          )}
          {error && <div className="err">{error}</div>}
        </div>
      )}

      {signedIn && (
        <>
          <div className="row" style={{ marginBottom: 14 }}>
            <div style={{ color: "var(--muted)", fontSize: 13 }}>
              {garments.length} {garments.length === 1 ? "item" : "items"}
            </div>
            <div style={{ flex: "0 0 auto" }}>
              <button className="ghost" onClick={() => void refresh()}>
                Refresh
              </button>
            </div>
          </div>

          {garments.length === 0 ? (
            <div className="empty">Nothing here yet. Add a photo above.</div>
          ) : (
            <div className="grid">
              {garments.map((g) => {
                const badge = badgeFor(g.state);
                return (
                  <div className="card" key={g.id}>
                    <div className="thumb">
                      {g.cutout_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={g.cutout_url} alt={g.subcategory ?? "garment"} />
                      ) : (
                        <span className="spinner" />
                      )}
                    </div>
                    <div className="meta">
                      <span className={`badge ${badge.cls}`}>{badge.label}</span>
                      <div style={{ marginTop: 6, color: "var(--muted)" }}>
                        {g.subcategory ?? g.slot ?? "untagged"}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </main>
  );
}
