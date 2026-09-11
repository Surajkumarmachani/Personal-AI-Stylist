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
  type CorrectionRate,
  type Garment,
  type JobEvent,
  correctionRate,
  ingest,
  listGarments,
  login,
  presign,
  register,
  setToken,
  streamJob,
  logWear,
  uploadToStorage,
} from "@/lib/api";
import Link from "next/link";

import GarmentEditor from "./GarmentEditor";
import WardrobeTools from "./WardrobeTools";

// jobs.state -> what the user is told.
//
// needs_review is a SEPARATE column from state, and a garment can be
// state="complete" AND needs_review=true — a field came back below its
// confidence threshold. Reading only `state` showed a green "ready" on items
// the pipeline had explicitly flagged, which hid the entire review gate: the
// low-confidence handling worked in the data and was invisible in the UI, so
// nobody would ever go correct the field it was asking about.
function badgeFor(state: string, needsReview = false): { cls: string; label: string } {
  if (needsReview && (state === "complete" || state === "matted")) {
    return { cls: "review", label: "needs review" };
  }
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
  const [uploadProgress, setUploadProgress] = useState<{ done: number; total: number } | null>(
    null,
  );
  const [garments, setGarments] = useState<Garment[]>([]);
  const [progress, setProgress] = useState<Record<string, JobEvent>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [rates, setRates] = useState<CorrectionRate | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const cleanups = useRef<Array<() => void>>([]);

  const refresh = useCallback(async () => {
    try {
      setGarments(await listGarments());
      // The correction rate is the live accuracy metric (§D1). Surfacing it in
      // the app rather than only on a dashboard means whoever is using it sees
      // extraction quality degrade at the same moment the users do.
      setRates(await correctionRate());
      // CLEAR on success. Without this an error outlives the condition it
      // describes: a failed call left "TypeError: Failed to fetch" on screen
      // permanently, so after the underlying problem was fixed the banner
      // still accused the app of being broken while the wardrobe loaded
      // perfectly well beneath it. A stale error is worse than none — it sends
      // you debugging something that already works.
      setError(null);
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
      // Uploads run CONCURRENTLY, capped at 4. Sequentially, 30 photos is 30
      // round trips of presign-then-PUT with a blank screen in between; the cap
      // exists because unbounded parallelism on a phone connection makes every
      // upload slower and can exhaust the browser's connection pool.
      const all = Array.from(files);
      const uploaded: { upload_id: string; key: string }[] = [];
      const failures: string[] = [];
      let done = 0;
      const CONCURRENCY = 4;
      const queue = [...all];

      async function worker() {
        for (;;) {
          const file = queue.shift();
          if (!file) return;
          try {
            const p = await presign(file.type || "image/jpeg");
            await uploadToStorage(p, file);
            uploaded.push({ upload_id: p.upload_id, key: p.key });
          } catch (e) {
            // One unreadable file must not discard the other 29 uploads. The
            // batch continues and the failures are reported at the end.
            failures.push(`${file.name}: ${String(e).slice(0, 80)}`);
          } finally {
            done += 1;
            setUploadProgress({ done, total: all.length });
          }
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(CONCURRENCY, all.length) }, () => worker()),
      );
      setUploadProgress(null);

      if (uploaded.length === 0) {
        throw new Error(`no files uploaded. ${failures.join("; ")}`);
      }
      if (failures.length > 0) {
        setError(`${failures.length} of ${all.length} failed: ${failures.join("; ")}`);
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
        Upload a photo — it is split into garments, cut out, coloured and tagged.
        Click any item to review or correct its tags.
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
          {uploadProgress && uploadProgress.total > 1 && (
            <div className="hint">
              Uploading {uploadProgress.done} of {uploadProgress.total}…
            </div>
          )}
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

          {rates && rates.by_field.length > 0 && (
            <div className="panel" style={{ padding: "12px 16px", marginBottom: 18 }}>
              <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
                Correction rate over {rates.window_days} days — how often each field needed fixing.
                Above ~20% on any field means ingestion needs work before anything is built on
                these tags.
              </div>
              <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 12 }}>
                {rates.by_field.map((f) => (
                  <span key={f.field}>
                    <code>{f.field}</code>{" "}
                    <strong
                      style={{
                        color: (f.rate ?? 0) > 0.2 ? "var(--bad)" : "var(--ok)",
                      }}
                    >
                      {f.rate === null ? "—" : `${(f.rate * 100).toFixed(0)}%`}
                    </strong>
                    <span style={{ color: "var(--muted)" }}> ({f.corrections})</span>
                  </span>
                ))}
              </div>
            </div>
          )}

          {editing && (
            <GarmentEditor
              garmentId={editing}
              onClose={() => setEditing(null)}
              onSaved={() => void refresh()}
            />
          )}

          <div className="searchrow" style={{ justifyContent: "flex-end" }}>
            {/* next/link, NOT <a href>. A plain anchor is a full page load,
                which discards the in-memory access token (see lib/api.ts on
                why it is not in localStorage) and lands you on a signed-out
                page. Client-side navigation keeps the session. */}
            <Link className="link" href="/eval">
              Model QA — what each model actually produced →
            </Link>
          </div>

          <WardrobeTools onChanged={() => void refresh()} />

          {garments.length === 0 ? (
            <div className="empty">Nothing here yet. Add a photo above.</div>
          ) : (
            <div className="grid">
              {garments.map((g) => {
                const badge = badgeFor(g.state, g.needs_review);
                return (
                  <div
                    className="card"
                    key={g.id}
                    onClick={() => setEditing(g.id)}
                    style={{ cursor: "pointer" }}
                    title="Click to review and correct this item's tags"
                  >
                    <div className="thumb">
                      {g.cutout_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={g.cutout_url} alt={g.subcategory ?? "garment"} />
                      ) : (
                        <span className="spinner" />
                      )}
                    </div>
                    <button

                      className="wear"

                      title="Log that you wore this today"

                      onClick={(e) => {

                        // The card opens the editor. Without this the click does both,

                        // and logging a wearing pops a dialog the user did not ask for.

                        e.stopPropagation();

                        void logWear(g.id).then(() => refresh());

                      }}

                    >

                      Worn today

                    </button>

                    <div className="meta">
                      <span className={`badge ${badge.cls}`}>{badge.label}</span>
                      <div style={{ marginTop: 6, color: "var(--muted)" }}>
                        {g.subcategory ?? g.slot ?? "untagged"}
                      </div>
                      {g.primary_colour && (
                        <div style={{ marginTop: 2, color: "var(--muted)", fontSize: 11 }}>
                          {g.primary_colour}
                        </div>
                      )}
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
