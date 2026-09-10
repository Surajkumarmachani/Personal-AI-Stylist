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
