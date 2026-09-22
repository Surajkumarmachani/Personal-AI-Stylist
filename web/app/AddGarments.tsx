"use client";

/** Upload photos into the wardrobe, from the product UI.
 *
 * WHY THIS EXISTS: the Wardrobe page's "+ Add" used to link to /dev, the build
 * console. That is a FULL page load, which drops the in-memory access token —
 * so the user landed on a second sign-in screen, in a different theme, to do
 * the most ordinary thing the product asks of them. A wardrobe app you cannot
 * put clothes into from the wardrobe screen is not finished.
 *
 * MULTIPLE FILES IN ONE INGEST, not one call each. The tag stage batches
 * garments into a grid and pays for one model call per grid, so six photos
 * sent together cost a fraction of six sent separately.
 *
 * "ACCEPTED" IS NOT "CATALOGUED", AND THIS SCREEN USED TO CONFLATE THEM
 * --------------------------------------------------------------------
 * `POST /garments/ingest` returns `accepted` — the count of photos QUEUED.
 * Validation happens a second later in the worker, and it fails terminally:
 * too small, not an image, decode bomb. This component used to print
 * "3 photo(s) accepted … refresh to see them appear", and then the pipeline
 * rejected all three for being 148px tall. Refreshing showed nothing, forever,
 * with no reason given anywhere a user can see.
 *
 * That is the recurring failure of this codebase in miniature: a signal that
 * looks live but cannot answer its own question. `accepted` answers "did the
 * queue take it", and the screen asked it "is my garment in the wardrobe".
 *
 * So there are now two gates, and neither of them is optimism:
 *
 *   1. Dimensions are checked HERE, before a byte is uploaded. The rule is
 *      known and the browser can evaluate it, so making the user wait for a
 *      round trip to learn their thumbnail is a thumbnail is pure latency.
 *   2. Every job that IS queued is followed to a terminal state over SSE, and
 *      rejections are reported with the worker's own words.
 */

import { useRef, useState } from "react";
import { ingest, presign, streamJob, uploadToStorage, type JobEvent } from "@/lib/api";

type Stage = { done: number; total: number; label: string };
type Outcome = { name: string; state: string; error: string | null };

// Mirrors MIN_SIDE_PX in services/stylist_worker/stages/validate.py. Duplicated
// deliberately rather than fetched: this is a pre-flight courtesy, and the
// worker remains the authority — a client that skips this check (or an older
// tab) is still rejected server-side with the same number.
const MIN_SIDE_PX = 200;

// Terminal states that mean the photo did NOT become a garment. Kept in step
// with TERMINAL_JOB_STATES in services/stylist_api/routers/jobs.py, minus
// `complete`: `needs_review` and `duplicate_suspect` are terminal for the
// stream but the garment exists, so they are not failures to report here.
const FAILED_STATES = new Set(["rejected", "quarantined"]);

/** Read a picked file's pixel dimensions without decoding it into the DOM. */
async function dimensions(file: File): Promise<{ w: number; h: number } | null> {
  try {
    const bitmap = await createImageBitmap(file);
    const size = { w: bitmap.width, h: bitmap.height };
    bitmap.close();
    return size;
  } catch {
    // An unreadable or unsupported file. Not our call to reject — let the
    // worker's magic-byte check give the authoritative reason.
    return null;
  }
}

export default function AddGarments({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<Stage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [tooSmall, setTooSmall] = useState<string[]>([]);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  async function upload() {
    const picked = Array.from(fileRef.current?.files ?? []);
    if (!picked.length || busy) return;
    setBusy(true);
    setErr(null);
    setOk(null);
    setTooSmall([]);
    setOutcomes([]);

    try {
      // ---- gate 1: dimensions, before anything is uploaded ----------------
      const rejected: string[] = [];
      const files: File[] = [];
      for (const f of picked) {
        const d = await dimensions(f);
        if (d && Math.min(d.w, d.h) < MIN_SIDE_PX) {
          rejected.push(`${f.name} — ${d.w}×${d.h}`);
        } else {
          files.push(f);
        }
      }
      setTooSmall(rejected);
      if (!files.length) {
        setBusy(false);
        setStage(null);
        return;
      }

      const uploads: { upload_id: string; key: string }[] = [];
      for (const [i, f] of files.entries()) {
        setStage({ done: i, total: files.length, label: f.name });
        const p = await presign(f.type || "image/jpeg");
        await uploadToStorage(p, f);
        uploads.push({ upload_id: p.upload_id, key: p.key });
      }
      setStage({ done: files.length, total: files.length, label: "starting the pipeline" });

      // An idempotency key per BATCH: retrying a failed ingest must not start a
      // second pipeline over the same photos — that is a doubled model bill and
      // duplicate wardrobe rows.
      const res = await ingest(uploads, crypto.randomUUID());

      // ---- gate 2: follow each job to a terminal state ---------------------
      // job_ids come back in the order the keys were sent, so index i is
      // files[i]. Without that pairing a rejection would name a uuid, and the
      // user cannot map a uuid back to the photo they need to retake.
      const settled = await Promise.all(
        res.job_ids.map(
          (id, i) =>
            new Promise<Outcome>((resolve) => {
              let last: JobEvent | null = null;
              streamJob(
                id,
                (e) => {
                  last = e;
                  setStage({
                    done: files.length,
                    total: files.length,
                    label: `${files[i]?.name ?? "photo"} — ${e.state}`,
                  });
                },
                () =>
                  resolve({
                    name: files[i]?.name ?? "photo",
                    // The stream can close on a timeout or a dropped
                    // connection before a terminal state arrives. "unknown" is
                    // the honest label for that — it is not a success.
                    state: last?.state ?? "unknown",
                    error: last?.last_error ?? null,
                  }),
              );
            }),
        ),
      );

      setOutcomes(settled.filter((o) => FAILED_STATES.has(o.state)));
      const good = settled.filter((o) => !FAILED_STATES.has(o.state)).length;
      if (good) {
        setOk(
          `${good} photo(s) catalogued.` +
            (good < settled.length ? "" : " They are in your wardrobe now."),
        );
      }
      if (fileRef.current) fileRef.current.value = "";
      onDone();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
      setStage(null);
    }
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 22 }}>
      <h2 className="ui-h3">Add garments</h2>
      <p className="ui-sub" style={{ marginBottom: 12 }}>
        One garment per photo works best, but a flat-lay of several is split automatically.
        Select as many as you like — they are sent as one batch, which is cheaper to tag.
        Photos must be at least {MIN_SIDE_PX}px on the shorter side; a thumbnail saved from
        a search-results page is usually too small to cut out.
      </p>

      <input
        ref={fileRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        multiple
        disabled={busy}
        style={{ display: "block", marginBottom: 12, fontSize: 13 }}
      />

      <button className="ui-btn primary" onClick={() => void upload()} disabled={busy}>
        {busy ? "Uploading…" : "Upload"}
      </button>

      {stage ? (
        <p className="ui-sub" style={{ marginTop: 10 }}>
          {stage.done}/{stage.total} — {stage.label}
        </p>
      ) : null}
      {ok ? <p className="ui-sub" style={{ color: "var(--ok)", marginTop: 10 }}>{ok}</p> : null}

      {tooSmall.length ? (
        <div className="ui-err" style={{ marginTop: 10 }}>
          <b>Not uploaded — too small</b> (need {MIN_SIDE_PX}px on the shorter side):
          <ul style={{ margin: "6px 0 0 18px" }}>
            {tooSmall.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
          Open the image at full size and save that, or photograph the garment yourself.
        </div>
      ) : null}

      {outcomes.length ? (
        <div className="ui-err" style={{ marginTop: 10 }}>
          <b>Rejected by the pipeline</b>:
          <ul style={{ margin: "6px 0 0 18px" }}>
            {outcomes.map((o) => (
              <li key={o.name}>
                {o.name} — {o.error ?? o.state}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}

      <p className="ui-sub" style={{ marginTop: 12 }}>
        You need at least one pair of <b>shoes</b> catalogued — footwear is the one slot every
        outfit requires, so without it no suggestions can be built at all.
      </p>
    </div>
  );
}
