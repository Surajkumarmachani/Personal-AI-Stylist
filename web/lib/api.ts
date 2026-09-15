// Thin API client. Deliberately no state-management library: Phase 2 has one
// screen, and the ceremony would outweigh the feature.

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

// Access tokens live 15 minutes and are held in memory only.
//
// NOT localStorage: anything in localStorage is readable by any script that
// ends up on the page, which turns one XSS into stolen credentials. Losing the
// token on refresh is the correct trade for a Phase 2 dev UI; Phase 8 replaces
// this with an httpOnly refresh cookie.
let accessToken: string | null = null;

export function setToken(token: string | null): void {
  accessToken = token;
}

export function getToken(): string | null {
  return accessToken;
}

function authHeaders(): HeadersInit {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}

export type Garment = {
  id: string;
  slot: string | null;
  subcategory: string | null;
  primary_colour: string | null;
  state: string;
  needs_review: boolean;
  cutout_url: string | null;
  created_at: string;
};

export type PresignResponse = {
  upload_id: string;
  key: string;
  url: string;
  fields: Record<string, string>;
  method: string;
  expires_at: string;
  max_bytes: number;
};

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${body.slice(0, 300)}`);
  }
  return (await resp.json()) as T;
}

export async function register(email: string, password: string) {
  return json<{ access_token: string; refresh_token: string }>(
    await fetch(`${API_BASE}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  );
}

export async function login(email: string, password: string) {
  return json<{ access_token: string; refresh_token: string }>(
    await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  );
}

export async function listGarments(): Promise<Garment[]> {
  return json<Garment[]>(
    await fetch(`${API_BASE}/garments`, { headers: authHeaders(), cache: "no-store" }),
  );
}

export async function presign(contentType: string): Promise<PresignResponse> {
  return json<PresignResponse>(
    await fetch(`${API_BASE}/uploads/presign`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ content_type: contentType }),
    }),
  );
}

/**
 * Upload straight to object storage.
 *
 * Presigned POST, not PUT: the size ceiling is a condition inside the signed
 * policy, so storage rejects an oversized body itself. The API is not in this
 * request path at all, which is the whole point — and also why the ceiling
 * cannot be enforced by a handler.
 */
export async function uploadToStorage(p: PresignResponse, file: File): Promise<void> {
  const form = new FormData();
  for (const [k, v] of Object.entries(p.fields)) form.append(k, v);
  form.append("file", file);

  const resp = await fetch(p.url, { method: "POST", body: form });
  if (!resp.ok) {
    const body = await resp.text();
    // A 400 here is usually the policy doing its job (too large, wrong type),
    // so say so rather than reporting a generic upload failure.
    throw new Error(
      `storage rejected the upload (${resp.status}). ` +
        `If this is a size limit, the policy caps at ${p.max_bytes} bytes. ${body.slice(0, 200)}`,
    );
  }
}

export async function ingest(
  uploads: { upload_id: string; key: string }[],
  idempotencyKey: string,
) {
  return json<{ job_ids: string[]; accepted: number; idempotent_replay: boolean }>(
    await fetch(`${API_BASE}/garments/ingest`, {
      method: "POST",
      headers: {
        ...authHeaders(),
        "Content-Type": "application/json",
        // Retrying an ingest must not start a second pipeline over the same
        // photos — a doubled VLM bill and duplicate wardrobe rows.
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        upload_ids: uploads.map((u) => u.upload_id),
        keys: uploads.map((u) => u.key),
      }),
    }),
  );
}

export type JobEvent = {
  job_id: string;
  state: string;
  garment_id: string | null;
  last_error: string | null;
  dlq: boolean;
};

/**
 * Subscribe to one job's progress.
 *
 * Uses fetch + a ReadableStream rather than EventSource because EventSource
 * cannot send an Authorization header — it would mean putting the access token
 * in the query string, where it lands in every proxy and access log.
 */
export function streamJob(
  jobId: string,
  onEvent: (e: JobEvent) => void,
  onDone: () => void,
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const resp = await fetch(`${API_BASE}/jobs/${jobId}/events`, {
        headers: authHeaders(),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        onDone();
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const eventLine = frame.split("\n").find((l) => l.startsWith("event:"));
          const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!dataLine) continue;
          const name = eventLine?.slice(6).trim();
          if (name === "state") onEvent(JSON.parse(dataLine.slice(5)) as JobEvent);
          if (name === "done" || name === "gone" || name === "timeout") {
            onDone();
            return;
          }
        }
      }
      onDone();
    } catch {
      // Aborted on unmount, or the network dropped. Either way the grid
      // refresh below is the fallback, so this is not surfaced as an error.
      onDone();
    }
  })();

  return () => controller.abort();
}

// ---------------------------------------------------------------- corrections

export type GarmentDetail = {
  garment: Record<string, unknown>;
  options: Record<string, (string | number)[]>;
  review_below: Record<string, number | null>;
};

export async function garmentDetail(id: string): Promise<GarmentDetail> {
  return json<GarmentDetail>(
    await fetch(`${API_BASE}/garments/${id}/detail`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

/**
 * Correct one field.
 *
 * The value set comes from `garmentDetail().options`, which the API builds from
 * taxonomy.yaml — so the UI can never offer a value the database would reject.
 * Free-text entry here would let a user "fix" a field into something no filter
 * matches, which looks to them like the fix silently failing.
 */
export async function correctField(
  garmentId: string,
  fieldName: string,
  newValue: string | number | null,
) {
  return json<{
    field_name: string;
    old_value: string | null;
    new_value: string | null;
    user_verified_fields: string[];
  }>(
    await fetch(`${API_BASE}/garments/${garmentId}/fields`, {
      method: "PATCH",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ field_name: fieldName, new_value: newValue }),
    }),
  );
}

export type CorrectionRate = {
  window_days: number;
  garments: number;
  by_field: {
    field: string;
    corrections: number;
    rate: number | null;
    avg_model_confidence: number | null;
  }[];
};

export async function correctionRate(): Promise<CorrectionRate> {
  return json<CorrectionRate>(
    await fetch(`${API_BASE}/ops/correction-rate`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

// ---------------------------------------------------------------- Phase 5

export type SearchResult = {
  id: string;
  slot: string | null;
  subcategory: string | null;
  primary_colour: string | null;
  dress_code: string | null;
  material: string | null;
  state: string;
  needs_review: boolean;
  needs_wash: boolean;
  duplicate_of: string | null;
  cutout_url: string | null;
  rank: number | null;
};

export type SearchFilters = {
  q?: string;
  slot?: string;
  primary_colour?: string;
  dress_code?: string;
  material?: string;
  needs_wash?: boolean;
  needs_review?: boolean;
};

export type Facets = {
  slot: Array<{ value: string; count: number }>;
  primary_colour: Array<{ value: string; count: number }>;
  dress_code: Array<{ value: string; count: number }>;
  material: Array<{ value: string; count: number }>;
  // Present for the preference picker rather than for search filtering.
  subcategory: Array<{ value: string; count: number }>;
  fit: Array<{ value: string; count: number }>;
  pattern: Array<{ value: string; count: number }>;
  flags: {
    needs_wash: number;
    needs_review: number;
    duplicate_suspect: number;
    total: number;
  };
};

export async function searchGarments(
  filters: SearchFilters,
): Promise<{ items: SearchResult[]; total: number }> {
  const params = new URLSearchParams();
  // Empty strings are dropped rather than sent: `?slot=` is a filter on the
  // empty string, which the API correctly rejects as an invalid enum, and the
  // user sees a 400 for having cleared a dropdown.
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "" && value !== null) {
      params.set(key, String(value));
    }
  }
  return json(
    await fetch(`${API_BASE}/garments/search?${params}`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

export async function facets(): Promise<Facets> {
  return json(
    await fetch(`${API_BASE}/wardrobe/facets`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

export type WearResponse = {
  garment_id: string;
  worn_on: string;
  total_wears: number;
  already_logged: boolean;
  cost_per_wear_minor: number | null;
  currency: string | null;
};

export async function logWear(id: string): Promise<WearResponse> {
  return json(
    await fetch(`${API_BASE}/garments/${id}/wear`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }),
  );
}

export async function setLaundry(id: string, needsWash: boolean): Promise<void> {
  await json(
    await fetch(`${API_BASE}/garments/${id}/laundry`, {
      method: "PATCH",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ needs_wash: needsWash }),
    }),
  );
}

export type MostWorn = {
  items: Array<{
    id: string;
    subcategory: string | null;
    primary_colour: string | null;
    wears: number;
    last_worn: string | null;
    cost_per_wear_minor: number | null;
    currency: string | null;
  }>;
  count: number;
};

export async function mostWorn(limit = 20): Promise<MostWorn> {
  return json(
    await fetch(`${API_BASE}/wardrobe/most-worn?limit=${limit}`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

export type DuplicatePair = {
  garment_id: string;
  subcategory: string | null;
  primary_colour: string | null;
  created_at: string;
  duplicate_of: {
    id: string;
    subcategory: string | null;
    primary_colour: string | null;
    created_at: string;
  };
};

export async function pendingDuplicates(): Promise<{ items: DuplicatePair[] }> {
  return json(
    await fetch(`${API_BASE}/wardrobe/duplicates`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

export async function resolveDuplicate(
  id: string,
  resolution: "different" | "same",
): Promise<void> {
  await json(
    await fetch(`${API_BASE}/garments/${id}/duplicate-resolution`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ resolution }),
    }),
  );
}

// ------------------------------------------------------------- model QA view

export type EvalField = {
  field: string;
  value: string | number | null;
  confidence: number | null;
  review_below: number | null;
  below_threshold: boolean;
  user_verified: boolean;
};

export type EvalItem = {
  id: string;
  created_at: string;
  state: string;
  needs_review: boolean;
  cutout_url: string | null;
  original_url: string | null;
  fields: EvalField[];
  climate_bands: string[];
  tag_source: string;
  tag_is_real: boolean;
  tag_degraded: boolean;
  tag_reason: string | null;
  extractor_version: string | null;
  embedding_version: string | null;
  has_embedding: boolean;
  phash: string | null;
  duplicate_of: string | null;
  moderation: Record<string, unknown>;
};

export type EvalPage = {
  items: EvalItem[];
  total: number;
  limit: number;
  offset: number;
  // What ACTUALLY tagged, read from `model_calls`, not from config — the two
  // can drift and the banner was wrong because of it.
  tagging_model: string;
  tagging_model_configured: string;
  // null = nothing tagged yet. "we have not run" and "we ran a mock" are
  // different facts and must not collapse into false.
  tagging_is_mock: boolean | null;
  tagging_config_disagrees: boolean;
};

export async function evalView(opts: {
  limit?: number;
  offset?: number;
  onlyReal?: boolean;
}): Promise<EvalPage> {
  const p = new URLSearchParams();
  p.set("limit", String(opts.limit ?? 24));
  p.set("offset", String(opts.offset ?? 0));
  if (opts.onlyReal) p.set("only_real", "true");
  return json(
    await fetch(`${API_BASE}/garments/eval?${p}`, {
      headers: authHeaders(),
      cache: "no-store",
    }),
  );
}

// ---------------------------------------------------------- preference facts
//
// The plan's argument for this feature is that "legibility buys trust faster
// than accuracy does" — a user who can SEE what the system believes about
// them, and correct it, forgives a wrong suggestion. That only holds while
// what is shown is also what is ENFORCED, which is why `never` is a hard
// filter in the candidate pool and `avoids` is a relaxable one.

export type PreferenceFact = {
  id: string;
  kind: "avoids" | "never" | "prefers";
  field_name: string;
  field_value: string;
  // "user" (they said so) or "inferred" (we guessed). Rendered differently:
  // presenting a guess as the user's own words is how you lose their trust in
  // one screen.
  source: string;
  created_at: string;
};

export const FACT_FIELDS = [
  "subcategory",
  "primary_colour",
  "material",
  "fit",
  "pattern",
] as const;

export async function listPreferences(): Promise<{ facts: PreferenceFact[] }> {
  return json(
    await fetch(`${API_BASE}/me/preferences`, { headers: authHeaders(), cache: "no-store" }),
  );
}

export async function addPreference(
  kind: PreferenceFact["kind"],
  fieldName: string,
  fieldValue: string,
): Promise<PreferenceFact> {
  return json(
    await fetch(`${API_BASE}/me/preferences`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ kind, field_name: fieldName, field_value: fieldValue }),
    }),
  );
}

export async function deletePreference(id: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/me/preferences/${id}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
}
