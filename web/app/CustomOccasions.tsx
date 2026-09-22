"use client";

/** Name your own occasions.
 *
 * WHAT THE USER IS ACTUALLY DOING HERE, and the copy says so: giving one of
 * the eighteen taxonomy occasions a name of their own, optionally dressier or
 * plainer. It is NOT a nineteenth occasion — taxonomy.yaml is frozen and its
 * enums generate Postgres types, so the scorer would have no formality or
 * dress-code target for a genuinely new one (see migration 0018).
 *
 * Pretending otherwise would be the more "magical" UI and would set up the
 * exact disappointment this screen exists to avoid: a custom occasion that
 * produces no outfits because nothing downstream knows what to aim at.
 */

import { useEffect, useState } from "react";
import { OCCASIONS } from "./OCCASIONS";
import {
  createCustomOccasion,
  deleteCustomOccasion,
  listCustomOccasions,
  type CustomOccasion,
} from "@/lib/api";

export default function CustomOccasions({ onChanged }: { onChanged?: () => void }) {
  const [items, setItems] = useState<CustomOccasion[]>([]);
  const [name, setName] = useState("");
  const [base, setBase] = useState(OCCASIONS[0]!.id);
  const [formality, setFormality] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function refresh() {
    listCustomOccasions()
      .then((r) => setItems(r.items))
      .catch(() => undefined);
  }
  useEffect(refresh, []);

  async function add() {
    if (!name.trim() || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await createCustomOccasion(name.trim(), base, formality ? Number(formality) : null);
      setName("");
      setFormality("");
      refresh();
      onChanged?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setBusy(true);
    try {
      await deleteCustomOccasion(id);
      refresh();
      onChanged?.();
    } finally {
      setBusy(false);
    }
  }

  const titleOf = (id: string) => OCCASIONS.find((o) => o.id === id)?.title ?? id;

  return (
    <div className="ui-panel" style={{ marginTop: 20 }}>
      <h2 className="ui-h3">Your own occasions</h2>
      <p className="ui-sub" style={{ marginBottom: 12 }}>
        Give one of the occasions above a name you actually use — &ldquo;Farmhouse
        haldi&rdquo;, &ldquo;Friday standup&rdquo; — and say it in the chat. Your name wins
        over the built-in keywords, so &ldquo;office party&rdquo; stops being read as
        &ldquo;office&rdquo;. Set a formality if yours runs dressier or plainer than most.
      </p>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add();
          }}
          placeholder="Farmhouse haldi"
          aria-label="Your name for this occasion"
          maxLength={60}
          disabled={busy}
          style={{ flex: "1 1 180px", fontSize: 13 }}
        />
        <select
          value={base}
          onChange={(e) => setBase(e.target.value)}
          aria-label="Closest built-in occasion"
          disabled={busy}
          style={{ fontSize: 13 }}
        >
          {OCCASIONS.map((o) => (
            <option key={o.id} value={o.id}>
              like {o.title}
            </option>
          ))}
        </select>
        <select
          value={formality}
          onChange={(e) => setFormality(e.target.value)}
          aria-label="Formality"
          disabled={busy}
          style={{ fontSize: 13 }}
        >
          <option value="">same formality</option>
          {[1, 2, 3, 4, 5].map((n) => (
            <option key={n} value={n}>
              formality {n}
            </option>
          ))}
        </select>
        <button className="ui-btn primary" onClick={() => void add()} disabled={busy || !name.trim()}>
          {busy ? "…" : "Add"}
        </button>
      </div>

      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}

      {items.length > 0 ? (
        <div className="ui-pills" style={{ marginTop: 12 }}>
          {items.map((it) => (
            <button
              key={it.id}
              className="ui-pill"
              disabled={busy}
              title={`Resolves to ${titleOf(it.base_occasion)}${
                it.formality_override ? `, formality ${it.formality_override}` : ""
              }. Tap to remove.`}
              onClick={() => void remove(it.id)}
            >
              {it.name} → {titleOf(it.base_occasion)}
              {it.formality_override ? ` · f${it.formality_override}` : ""}
              <span aria-hidden="true"> ×</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
