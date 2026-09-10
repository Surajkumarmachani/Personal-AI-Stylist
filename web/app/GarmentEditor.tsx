"use client";

/**
 * Correction panel for one garment (Step 4.3).
 *
 * Every field is editable and every value comes from a dropdown built from
 * taxonomy.yaml, so the UI cannot offer something the database will reject.
 *
 * Three things the UI has to make visible, because they are the whole reason
 * corrections are worth collecting:
 *
 *   - WHICH fields the model was unsure about, and how unsure. A review badge
 *     with no explanation tells the user something is wrong without telling
 *     them where to look.
 *   - WHICH fields they have already verified. Those are locked against any
 *     future backfill, and knowing that is what makes the effort feel worth
 *     spending.
 *   - WHEN tagging did not happen at all. A degraded item is not a broken one;
 *     saying so avoids the user "fixing" empty fields that are about to fill
 *     in on their own.
 */

import { useCallback, useEffect, useState } from "react";
import { correctField, garmentDetail, type GarmentDetail } from "@/lib/api";

const FIELDS = [
  "slot",
  "subcategory",
  "primary_colour",
  "secondary_colour",
  "pattern",
  "material",
  "fit",
  "dress_code",
  "formality",
  "warmth",
] as const;

export default function GarmentEditor({
  garmentId,
  onClose,
  onSaved,
}: {
  garmentId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [detail, setDetail] = useState<GarmentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await garmentDetail(garmentId));
    } catch (e) {
      setError(String(e));
    }
  }, [garmentId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(field: string, raw: string) {
    setSaving(field);
    setError(null);
    try {
      // formality and warmth are integers; the rest are enum strings. An empty
      // selection means null — "this garment has no secondary colour" is a
      // legitimate correction, not a missing answer.
      const value =
        raw === ""
          ? null
          : field === "formality" || field === "warmth"
            ? Number(raw)
            : raw;
      await correctField(garmentId, field, value);
      await load();
      onSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(null);
    }
  }

  if (error && !detail) return <div className="err">{error}</div>;
  if (!detail) return <div className="panel">Loading…</div>;

  const g = detail.garment as Record<string, string | number | null>;
  const confidence = (g.field_confidence ?? {}) as Record<string, number>;
  const verified = (g.user_verified_fields ?? []) as unknown as string[];
  // `->>` in SQL yields text, so this is the string "true" — never a boolean.
  const degraded = g.tag_degraded === "true";

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 16 }}>
        <div>
          <strong>Edit garment</strong>
          <div style={{ color: "var(--muted)", fontSize: 12 }}>
            {String(g.extractor_version ?? "not yet tagged")}
          </div>
        </div>
        <div style={{ flex: "0 0 auto" }}>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>

      {degraded && (
        <div className="progress" style={{ marginBottom: 14 }}>
          AI tagging has not run for this item yet ({String(g.tag_reason ?? "unavailable")}). It
          stays fully usable, and tags will fill in automatically — no need to enter them by hand.
        </div>
      )}

      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <tbody>
          {FIELDS.map((field) => {
            const options = detail.options[field] ?? [];
            const conf = confidence[field];
            const threshold = detail.review_below[field];
            const isLow = conf !== undefined && threshold != null && conf < threshold;
            const isVerified = verified.includes(field);
            return (
              <tr key={field} style={{ borderTop: "1px solid var(--line)" }}>
                <td style={{ padding: "8px 6px", color: "var(--muted)", width: 150 }}>
                  {field.replace(/_/g, " ")}
                </td>
                <td style={{ padding: "8px 6px" }}>
                  <select
                    value={g[field] === null || g[field] === undefined ? "" : String(g[field])}
                    disabled={saving === field}
                    onChange={(e) => void save(field, e.target.value)}
                    style={{
                      width: "100%",
                      padding: "6px 8px",
                      border: "1px solid var(--line)",
                      borderRadius: 6,
                      background: "#fff",
                      font: "inherit",
                    }}
                  >
                    <option value="">— not set —</option>
                    {options.map((opt) => (
                      <option key={String(opt)} value={String(opt)}>
                        {String(opt)}
                      </option>
                    ))}
                  </select>
                </td>
                <td style={{ padding: "8px 6px", width: 150, textAlign: "right" }}>
                  {isVerified ? (
                    <span className="badge ready" title="You set this. A backfill will never overwrite it.">
                      yours
                    </span>
                  ) : conf !== undefined ? (
                    <span
                      className={`badge ${isLow ? "review" : "processing"}`}
                      title={
                        isLow
                          ? `The model was only ${(conf * 100).toFixed(0)}% confident (below the ${(
                              (threshold ?? 0) * 100
                            ).toFixed(0)}% review threshold)`
                          : `Model confidence ${(conf * 100).toFixed(0)}%`
                      }
                    >
                      {(conf * 100).toFixed(0)}%
                    </span>
                  ) : (
                    <span style={{ color: "var(--line)" }}>—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {error && <div className="err">{error}</div>}
      <div className="progress" style={{ marginTop: 12 }}>
        Anything you set here is locked — a model upgrade or backfill will not overwrite it.
      </div>
    </div>
  );
}
