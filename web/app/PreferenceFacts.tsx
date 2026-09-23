"use client";

/**
 * Preference facts — what the system believes about you, in your words.
 *
 * WHY THIS SCREEN EXISTS
 * ----------------------
 * The plan's argument: "legibility buys trust faster than accuracy does". A
 * user who can SEE what the system believes, and correct it, forgives a wrong
 * suggestion. A learned style vector cannot be shown to anyone; these can.
 *
 * WHAT IT MUST NOT DO
 * -------------------
 * It must not display a rule the pipeline does not apply. That was the state
 * of this feature until the candidate pool learned to obey these facts — an
 * endpoint that stored "never yellow" while yellow kept being suggested. So
 * every row here says what it ACTUALLY does ("hidden from suggestions" vs
 * "avoided when possible"), and the wording tracks the enforcement rather than
 * the intention.
 *
 * `source` is rendered differently on purpose. Telling someone "you never wear
 * yellow" in their own voice when they never said it is how you lose their
 * trust in one screen, so an inferred fact is labelled as our guess and is
 * dismissable on the same terms.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  addPreference,
  deletePreference,
  facets,
  listPreferences,
  type Facets,
  type PreferenceFact,
} from "../lib/api";

// The three kinds, with the words the UI uses. Copy lives next to the enum so
// a new kind cannot be added to the API without someone deciding what it says
// to a user — and, more importantly, what it does.
const KINDS = {
  never: {
    label: "Never",
    verb: "Never suggest",
    effect: "Hidden from suggestions entirely.",
    tone: "never",
  },
  avoids: {
    label: "Avoid",
    verb: "Avoid",
    effect: "Skipped when there's an alternative.",
    tone: "avoids",
  },
  prefers: {
    label: "Prefer",
    verb: "Prefer",
    // HONEST ABOUT NOT BEING WIRED. `prefers` is stored and shown but does not
    // yet change ranking: it needs a scorer weight, and the scorer's weights
    // are validated to sum to 1.0, so adding one is a deliberate rebalance
    // rather than a line of code. Saying "coming soon" beats implying an
    // effect that is not there.
    effect: "Saved — not yet used for ranking.",
    tone: "prefers",
  },
} as const;

type Kind = keyof typeof KINDS;

const FIELD_LABELS: Record<string, string> = {
  subcategory: "Garment type",
  primary_colour: "Colour",
  material: "Material",
  fit: "Fit",
  pattern: "Pattern",
};

function pretty(value: string): string {
  return value.replace(/_/g, " ");
}

export default function PreferenceFacts() {
  const [factsList, setFactsList] = useState<PreferenceFact[]>([]);
  const [options, setOptions] = useState<Facets | null>(null);
  const [kind, setKind] = useState<Kind>("never");
  const [field, setField] = useState<string>("primary_colour");
  const [value, setValue] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetching and applying are SPLIT so the mount effect and the
  // post-mutation refresh share one network path, while the effect keeps a
  // cancellation guard the click handler does not need. Calling the memoised
  // loader straight from the effect would trip set-state-in-effect: the rule
  // cannot see inside a callback to tell whether its setState is synchronous.
  const fetchAll = useCallback(() => Promise.all([listPreferences(), facets()]), []);

  const apply = useCallback((r: Awaited<ReturnType<typeof fetchAll>>) => {
    const [prefs, f] = r;
    setFactsList(prefs.facts);
    setOptions(f);
    setError(null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      apply(await fetchAll());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [fetchAll, apply]);

  useEffect(() => {
    let off = false;
    void (async () => {
      try {
        const r = await fetchAll();
        if (!off) apply(r);
      } catch (e) {
        if (!off) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      off = true;
    };
  }, [fetchAll, apply]);

  // Only values the user ACTUALLY OWNS. A picker listing all 144 subcategories
  // invites a rule about a garment type they do not have, which then does
  // nothing and teaches them the feature is decorative.
  const values = useMemo(() => {
    if (!options) return [];
    const list = (options as unknown as Record<string, Array<{ value: string; count: number }>>)[
      field
    ];
    return list ?? [];
  }, [options, field]);

  // Adjusted DURING RENDER, not in an effect. This is the pattern React
  // documents for "reset state when something changes": an effect renders
  // once with the stale selection and then immediately renders again, so for
  // one frame the form shows a value belonging to the PREVIOUS field — which
  // is the exact thing this reset exists to prevent. Comparing against the
  // previous `values` keeps the trigger identical, because `values` is
  // memoised on exactly [options, field].
  const [prevValues, setPrevValues] = useState(values);
  if (prevValues !== values) {
    setPrevValues(values);
    setValue(values[0]?.value ?? "");
  }

  const add = async () => {
    if (!value) return;
    setBusy(true);
    try {
      await addPreference(kind, field, value);
      await refresh();
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    try {
      await deletePreference(id);
      setFactsList((prev) => prev.filter((f) => f.id !== id));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const grouped = useMemo(() => {
    const out: Record<Kind, PreferenceFact[]> = { never: [], avoids: [], prefers: [] };
    for (const f of factsList) {
      if (f.kind in out) out[f.kind as Kind].push(f);
    }
    return out;
  }, [factsList]);

  return (
    <section className="panel">
      <h2>What we believe about your style</h2>
      <p className="muted">
        These rules change what gets suggested. Edit them freely — you know your wardrobe
        better than we do.
      </p>

      {error && <p className="error">{error}</p>}

      <div className="pref-add">
        <label>
          <span className="muted">Rule</span>
          <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            {Object.entries(KINDS).map(([k, meta]) => (
              <option key={k} value={k}>
                {meta.verb}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="muted">Attribute</span>
          <select value={field} onChange={(e) => setField(e.target.value)}>
            {Object.keys(FIELD_LABELS).map((f) => (
              <option key={f} value={f}>
                {FIELD_LABELS[f]}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="muted">Value</span>
          <select value={value} onChange={(e) => setValue(e.target.value)} disabled={!values.length}>
            {values.map((v) => (
              <option key={v.value} value={v.value}>
                {pretty(v.value)} ({v.count})
              </option>
            ))}
          </select>
        </label>

        <button onClick={add} disabled={busy || !value}>
          Add rule
        </button>
      </div>

      {/* The counts above are why an empty list is explained rather than just
          being empty: a user whose wardrobe has no `pattern` values yet would
          otherwise see a dead dropdown and no reason for it. */}
      {options && !values.length && (
        <p className="muted small">
          Nothing in your wardrobe has a {FIELD_LABELS[field].toLowerCase()} yet — catalogue a
          few more garments and the options will appear.
        </p>
      )}

      {factsList.length === 0 ? (
        <p className="muted">
          No rules yet. Add one above, or they&apos;ll build up as you react to suggestions.
        </p>
      ) : (
        <div className="pref-groups">
          {(Object.keys(KINDS) as Kind[]).map((k) =>
            grouped[k].length ? (
              <div key={k} className="pref-group">
                <h3>
                  {KINDS[k].label}
                  <span className="muted small"> — {KINDS[k].effect}</span>
                </h3>
                <ul className="pref-list">
                  {grouped[k].map((f) => (
                    <li key={f.id} className={`pref-chip pref-${KINDS[k].tone}`}>
                      <span className="pref-field">{FIELD_LABELS[f.field_name] ?? f.field_name}</span>
                      <strong>{pretty(f.field_value)}</strong>
                      {/* An inferred fact is OUR guess and is labelled as one.
                          Presenting it in the user's own voice would be
                          putting words in their mouth. */}
                      {f.source === "inferred" && (
                        <span className="pref-inferred" title="We guessed this from your feedback">
                          we guessed
                        </span>
                      )}
                      <button
                        className="pref-remove"
                        onClick={() => remove(f.id)}
                        disabled={busy}
                        aria-label={`Remove rule: ${KINDS[k].verb} ${pretty(f.field_value)}`}
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null,
          )}
        </div>
      )}
    </section>
  );
}
