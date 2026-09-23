"use client";

/**
 * Phase 5 surfaces: duplicate review, search + filters, and the most-worn
 * ranking the onboarding flow is built on.
 *
 * Duplicates come FIRST and unconditionally. The pipeline never merges — it
 * parks a garment and asks — so an unanswered question is the one thing here
 * that blocks the wardrobe from being correct. Burying it under a filter the
 * user has to discover would make "never auto-merge" a way of quietly losing
 * garments instead of a safety property.
 */

import { useCallback, useEffect, useState } from "react";
import {
  type DuplicatePair,
  type Facets,
  type MostWorn,
  type SearchFilters,
  type SearchResult,
  facets as fetchFacets,
  mostWorn as fetchMostWorn,
  pendingDuplicates,
  resolveDuplicate,
  searchGarments,
} from "@/lib/api";

function money(minor: number | null, currency: string | null): string {
  if (minor === null) return "—";
  // Minor units in, major units out. The API never sends a float for money.
  const major = (minor / 100).toFixed(2);
  return `${currency === "INR" ? "₹" : ""}${major}`;
}

export default function WardrobeTools({ onChanged }: { onChanged: () => void }) {
  const [dupes, setDupes] = useState<DuplicatePair[]>([]);
  const [facetData, setFacetData] = useState<Facets | null>(null);
  const [filters, setFilters] = useState<SearchFilters>({});
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [total, setTotal] = useState(0);
  const [ranking, setRanking] = useState<MostWorn | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetching and applying are SPLIT so the mount effect and the
  // post-mutation refresh share one network path, while the effect keeps a
  // cancellation guard the click handler does not need. Calling the memoised
  // loader straight from the effect would trip set-state-in-effect: the rule
  // cannot see inside a callback to tell whether its setState is synchronous.
  const fetchAll = useCallback(
    () => Promise.all([pendingDuplicates(), fetchFacets(), fetchMostWorn(20)]),
    [],
  );

  const apply = useCallback((r: Awaited<ReturnType<typeof fetchAll>>) => {
    const [d, f, m] = r;
    setDupes(d.items);
    setFacetData(f);
    setRanking(m);
    setError(null);
  }, []);

  const reload = useCallback(async () => {
    try {
      apply(await fetchAll());
    } catch (e) {
      setError(String(e));
    }
  }, [fetchAll, apply]);

  useEffect(() => {
    let off = false;
    void (async () => {
      try {
        const r = await fetchAll();
        if (!off) apply(r);
      } catch (e) {
        if (!off) setError(String(e));
      }
    })();
    return () => {
      off = true;
    };
  }, [fetchAll, apply]);

  const runSearch = useCallback(
    async (next: SearchFilters) => {
      setFilters(next);
      // An empty filter set means "no search", not "search for nothing" — show
      // the normal wardrobe grid rather than a redundant copy of it.
      const active = Object.values(next).some((v) => v !== undefined && v !== "");
      if (!active) {
        setResults(null);
        return;
      }
      try {
        const r = await searchGarments(next);
        setResults(r.items);
        setTotal(r.total);
        setError(null);
      } catch (e) {
        setError(String(e));
      }
    },
    [],
  );

  async function answer(id: string, resolution: "different" | "same") {
    setBusy(true);
    try {
      await resolveDuplicate(id, resolution);
      await reload();
      onChanged();
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="tools">
      {error && <div className="err">{error}</div>}

      {/* ---- duplicates: a question, never a decision already taken ---- */}
      {dupes.length > 0 && (
        <div className="panel warn">
          <h3>
            {dupes.length} possible duplicate{dupes.length === 1 ? "" : "s"}
          </h3>
          <p className="hint">
            These look like something you already have. Nothing has been merged — we
            never merge on our own.
          </p>
          {dupes.map((d) => (
            <div className="dupe" key={d.garment_id}>
              <span>
                <strong>{d.subcategory ?? "item"}</strong> in {d.primary_colour ?? "—"}{" "}
                <span className="muted">
                  (added {new Date(d.created_at).toLocaleDateString()})
                </span>{" "}
                looks like one from{" "}
                {new Date(d.duplicate_of.created_at).toLocaleDateString()}
              </span>
              <span className="actions">
                <button disabled={busy} onClick={() => void answer(d.garment_id, "same")}>
                  Same item
                </button>
                <button
                  disabled={busy}
                  onClick={() => void answer(d.garment_id, "different")}
                >
                  Different
                </button>
              </span>
            </div>
          ))}
        </div>
      )}

      {/* ---- search + filters ---- */}
      <div className="panel">
        <div className="searchrow">
          <input
            type="search"
            placeholder="Search your wardrobe — try a colour, fabric or type"
            value={filters.q ?? ""}
            onChange={(e) => void runSearch({ ...filters, q: e.target.value })}
          />
          {facetData && (
            <>
              <select
                value={filters.slot ?? ""}
                onChange={(e) => void runSearch({ ...filters, slot: e.target.value })}
              >
                <option value="">Any type</option>
                {/* Only values that MATCH something. A taxonomy-driven list
                    offers 144 subcategories to someone with nine garments. */}
                {facetData.slot.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.value} ({o.count})
                  </option>
                ))}
              </select>
              <select
                value={filters.primary_colour ?? ""}
                onChange={(e) =>
                  void runSearch({ ...filters, primary_colour: e.target.value })
                }
              >
                <option value="">Any colour</option>
                {facetData.primary_colour.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.value} ({o.count})
                  </option>
                ))}
              </select>
              <label className="chk">
                <input
                  type="checkbox"
                  checked={filters.needs_wash === true}
                  onChange={(e) =>
                    void runSearch({
                      ...filters,
                      needs_wash: e.target.checked ? true : undefined,
                    })
                  }
                />
                In the wash ({facetData.flags.needs_wash})
              </label>
            </>
          )}
          {results !== null && (
            <button onClick={() => void runSearch({})} className="link">
              Clear
            </button>
          )}
        </div>

        {results !== null && (
          <div className="results">
            <div className="hint">
              {total} match{total === 1 ? "" : "es"}
            </div>
            <div className="grid">
              {results.map((r) => (
                <div className="card" key={r.id}>
                  <div className="thumb">
                    {r.cutout_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={r.cutout_url} alt={r.subcategory ?? "garment"} />
                    ) : (
                      <span className="spinner" />
                    )}
                  </div>
                  <div className="meta">
                    <div>{r.subcategory ?? "—"}</div>
                    <div className="muted">{r.primary_colour ?? "—"}</div>
                    {r.needs_wash && <span className="badge review">in the wash</span>}
                  </div>
                </div>
              ))}
            </div>
            {results.length === 0 && (
              <div className="empty">Nothing matches those filters.</div>
            )}
          </div>
        )}
      </div>

      {/* ---- most worn ---- */}
      {ranking && ranking.items.some((i) => i.wears > 0) && (
        <div className="panel">
          <h3>Most worn</h3>
          <p className="hint">
            Cost per wear falls every time you wear something. This is the number that
            tells you which purchases actually earned their place.
          </p>
          <ol className="mostworn">
            {ranking.items
              .filter((i) => i.wears > 0)
              .map((i) => (
                <li key={i.id}>
                  <span>
                    {i.subcategory ?? "item"} · {i.primary_colour ?? "—"}
                  </span>
                  <span className="muted">
                    {i.wears} wear{i.wears === 1 ? "" : "s"}
                    {i.cost_per_wear_minor !== null &&
                      ` · ${money(i.cost_per_wear_minor, i.currency)} per wear`}
                  </span>
                </li>
              ))}
          </ol>
        </div>
      )}
    </div>
  );
}
