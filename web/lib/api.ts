// Thin API client. Deliberately no state-management library: Phase 2 has one
// screen, and the ceremony would outweigh the feature.

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

// SESSION HANDLING, AND WHY IT IS SPLIT IN TWO.
//
// The ACCESS token lives 15 minutes and stays in memory only — never in any
// web storage, because anything in storage is readable by any script that ends
// up on the page, and that turns one XSS into a usable credential.
//
// The REFRESH token goes in sessionStorage, and that is a deliberate trade
// rather than a relaxation of the rule above:
//
//   - The API returns it in the login BODY, not as an httpOnly cookie, so the
//     client is the only thing that can hold it. There is no option where the
//     browser keeps it out of reach of script.
//   - `/auth/refresh` ROTATES: the presented token is revoked as it is
//     exchanged (see routers/auth.py), so a stolen copy is single-use and its
//     reuse is detectable server-side.
//   - sessionStorage, not localStorage: it dies with the tab, so the window of
//     exposure is a session rather than forever.
//
// WHAT THIS FIXES. Before, the access token was in memory and the EMAIL was in
// sessionStorage, so after a reload the app believed it was signed in and
// every request 401'd with "missing bearer token" — a UI that looked
// authenticated over a session that did not exist. The gate now hangs on the
// token, which is the thing that actually decides whether a call will work.
const REFRESH_KEY = "stylist.refresh";

let accessToken: string | null = null;

function readRefresh(): string | null {
  try {
    return sessionStorage.getItem(REFRESH_KEY);
  } catch {
    // Private mode, blocked storage. The app still works; it just forgets
    // between page loads, which is the old behaviour rather than a break.
    return null;
  }
}

function writeRefresh(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(REFRESH_KEY, token);
    else sessionStorage.removeItem(REFRESH_KEY);
  } catch {
    /* see readRefresh */
  }
}

export function setToken(token: string | null): void {
  accessToken = token;
}

export function getToken(): string | null {
  return accessToken;
}

export function setSession(access: string, refresh?: string | null): void {
  accessToken = access;
  if (refresh !== undefined) writeRefresh(refresh);
}

export function clearSession(): void {
  accessToken = null;
  writeRefresh(null);
}

/** Sign out: revoke the refresh token server-side, then forget it locally.
 *
 *  BOTH HALVES MATTER. `clearSession()` alone only forgets the token in this
 *  browser -- the refresh token stays valid until it expires, so anyone
 *  holding a copy could still mint access tokens. Revoking without clearing
 *  leaves a dead token in storage that fails confusingly on next load.
 *
 *  The revoke is best-effort: if the network is down the local clear MUST
 *  still happen, because a sign-out that visibly does nothing is worse than
 *  one that is incomplete server-side.
 */
export async function signOut(): Promise<void> {
  const refresh = readRefresh();
  if (refresh) {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
    } catch {
      /* see the docstring: the local clear below is the part that must happen */
    }
  }
  clearSession();
}

export function hasSession(): boolean {
  return accessToken !== null || readRefresh() !== null;
}

/** Exchange the stored refresh token for a new access token.
 *
 *  Stores the ROTATED refresh token that comes back — the old one is already
 *  revoked server-side, so keeping it would make the next refresh fail with a
 *  401 that looks like an expired session and is actually our own bug.
 *
 *  Returns false when there is nothing to restore, which the caller reads as
 *  "show sign-in". */
export async function refreshSession(): Promise<boolean> {
  const refresh = readRefresh();
  if (!refresh) return false;
  try {
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) {
      // Expired, revoked, or reused. Clearing is the honest response: a stale
      // refresh token that stays put makes every page retry and fail forever.
      clearSession();
      return false;
    }
    const body = (await res.json()) as { access_token: string; refresh_token?: string };
    setSession(body.access_token, body.refresh_token ?? refresh);
    return true;
  } catch {
    return false;
  }
}

function authHeaders(): HeadersInit {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}

/** fetch with the bearer token, refreshing ONCE on a 401.
 *
 *  Access tokens last 15 minutes, so any session left open over lunch would
 *  otherwise start failing mid-use. Retrying once after a refresh turns that
 *  into something the user never sees.
 *
 *  Exactly once: if the retry also 401s the session is genuinely gone, and
 *  looping would hammer the endpoint on every dead session. */
export async function authedFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const withAuth = (): RequestInit => ({
    ...init,
    headers: { ...(init.headers ?? {}), ...authHeaders() },
  });

  if (!accessToken && readRefresh()) await refreshSession();

  let res = await fetch(input, withAuth());
  if (res.status === 401 && readRefresh()) {
    if (await refreshSession()) res = await fetch(input, withAuth());
  }
  return res;
}

export type Garment = {
  id: string;
  slot: string | null;
  subcategory: string | null;
  primary_colour: string | null;
  state: string;
  needs_review: boolean;
  /** In the wash. A HARD exclusion in the candidate pool, so a garment with
   *  this set cannot appear in any suggestion — which made "why is my
   *  favourite shirt never suggested?" unanswerable from the app. */
  needs_wash: boolean;
  /** `novelty` is 10% of the outfit score and reads exactly these. */
  wear_count: number;
  last_worn: string | null;
  /** Free text, user-entered, NOT scored and never inferred from a photo. */
  brand: string | null;
  size_label: string | null;
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

/** Whose clothes to suggest BUYING. Never filters the user's own wardrobe. */
export type DressesAs = "women" | "men" | "all";

export async function register(email: string, password: string, dressesAs?: DressesAs) {
  const body = await json<{ access_token: string; refresh_token: string }>(
    await fetch(`${API_BASE}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, dresses_as: dressesAs }),
    }),
  );
  setSession(body.access_token, body.refresh_token);
  return body;
}

export async function login(email: string, password: string) {
  const body = await json<{ access_token: string; refresh_token: string }>(
    await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  );
  // Stored HERE rather than left to each caller. A screen that signed in and
  // forgot to keep the refresh token would work until the next page load and
  // then 401 — which is exactly the bug this replaced.
  setSession(body.access_token, body.refresh_token);
  return body;
}

export type HomeLocation = {
  place: string | null;
  latitude: number | null;
  longitude: number | null;
  timezone: string | null;
  /** Stated by the server. Do not infer it from `place` being non-null —
   *  the coordinates are what drive the forecast, and only the server knows
   *  whether they are set. */
  weather_is_real?: boolean;
};

export type LocationChoice = HomeLocation & {
  /** Same-named cities the geocoder also matched, so a wrong resolution is
   *  correctable. "Bangalore" resolves ONLY to Bangalore Town, Sindh,
   *  Pakistan — the Indian city is indexed as Bengaluru — so this list is
   *  the difference between a visible mistake and a silently wrong forecast. */
  alternatives: { place: string; index: number }[];
};

export async function getLocation(): Promise<HomeLocation> {
  const res = await authedFetch(`${API_BASE}/me/location`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as HomeLocation;
}

export async function setLocation(place: string, choice = 0): Promise<LocationChoice> {
  const res = await authedFetch(`${API_BASE}/me/location`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ place, choice }),
  });
  if (!res.ok) {
    // 404 is a typo and 503 is the provider; the two need different advice,
    // so the status is carried rather than flattened to "failed".
    const detail = await res.text();
    throw new Error(
      res.status === 404
        ? `No city matched that. Check the spelling — some cities are indexed under another name (Bengaluru, not Bangalore).`
        : res.status === 503
          ? `Couldn't look that up right now. Try again in a moment.`
          : `HTTP ${res.status} ${detail.slice(0, 120)}`,
    );
  }
  return (await res.json()) as LocationChoice;
}

export async function clearLocation(): Promise<void> {
  const res = await authedFetch(`${API_BASE}/me/location`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

export type CustomOccasion = {
  id: string;
  name: string;
  base_occasion: string;
  formality_override: number | null;
  dress_code_override: string | null;
};

export async function listCustomOccasions(): Promise<{ items: CustomOccasion[] }> {
  const res = await authedFetch(`${API_BASE}/me/occasions`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as { items: CustomOccasion[] };
}

export async function createCustomOccasion(
  name: string,
  baseOccasion: string,
  formality?: number | null,
): Promise<CustomOccasion> {
  const res = await authedFetch(`${API_BASE}/me/occasions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      base_occasion: baseOccasion,
      formality_override: formality ?? null,
    }),
  });
  if (!res.ok) {
    // 409 is a name you already used and 400 is an unknown base — different
    // fixes, so they get different sentences rather than "failed".
    const detail = await res.text();
    throw new Error(
      res.status === 409
        ? `You already have an occasion called "${name}".`
        : res.status === 400
          ? `That base occasion isn't one the scorer knows.`
          : `HTTP ${res.status} ${detail.slice(0, 100)}`,
    );
  }
  return (await res.json()) as CustomOccasion;
}

export async function deleteCustomOccasion(id: string): Promise<void> {
  const res = await authedFetch(`${API_BASE}/me/occasions/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

// ------------------------------------------------- insights & privacy
//
// These endpoints all existed and NOTHING in the product called them. The
// style vector and wear-through rate had no screen at all; export and account
// deletion had no screen either, which for deletion is a compliance gap and
// not merely a missing feature — the erasure saga is built and audited, and
// the user has to be able to start it.

export type StyleSummary = {
  events_applied: number;
  last_event_id: string | null;
  alpha: number;
  dimensions: number;
  feedback_by_kind: Record<string, number>;
};

export async function styleSummary(): Promise<StyleSummary> {
  return json(await authedFetch(`${API_BASE}/me/style`, { cache: "no-store" }));
}

export type WearThrough = {
  suggested_outfits: number;
  worn_outfits: number;
  wear_through_rate: number | null;
  window_days: number;
};

export async function wearThrough(): Promise<WearThrough> {
  return json(await authedFetch(`${API_BASE}/me/wear-through`, { cache: "no-store" }));
}

export async function requestExport(): Promise<{ export_id: string; state: string }> {
  const res = await authedFetch(`${API_BASE}/me/export`, { method: "POST" });
  if (!res.ok) {
    // 409 means one is already running — that is not an error to shout about.
    throw new Error(
      res.status === 409
        ? "An export is already being prepared. Check back shortly."
        : `HTTP ${res.status}`,
    );
  }
  return (await res.json()) as { export_id: string; state: string };
}

export type ExportStatus = {
  id: string;
  state: string;
  size_bytes: number | null;
  download_url?: string | null;
  last_error?: string | null;
};

export async function exportStatus(id: string): Promise<ExportStatus> {
  return json(await authedFetch(`${API_BASE}/me/export/${id}`, { cache: "no-store" }));
}

/** Start the erasure saga. Irreversible.
 *
 * `?confirm=DELETE` is required by the API and is NOT a formality the client
 * should paper over — the endpoint's own comment calls it "one stray request
 * away from deleting an account". The UI asks for the word too, rather than
 * sending it on the user's behalf behind a single button. */
export async function deleteAccount(): Promise<{ state: string }> {
  const res = await authedFetch(`${API_BASE}/me?confirm=DELETE`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${(await res.text()).slice(0, 120)}`);
  return (await res.json()) as { state: string };
}

// ------------------------------------------------------------- calendar
//
// Five endpoints with real Google OAuth, and nothing in the product called
// any of them. Note what `/calendar/today` returns: a CLASSIFICATION and a
// COUNT of events, never the titles — the backend deliberately never sends
// calendar contents to the client and never stores them.

export type CalendarStatus = {
  connected: boolean;
  account_email?: string | null;
  scope?: string | null;
  connected_at?: string | null;
  last_synced_at?: string | null;
  revoked_at?: string | null;
};

export type CalendarToday = {
  occasion: string;
  dress_code: string | null;
  formality_target: number | null;
  confidence: number;
  /** The gate as its own flag, so the client does not have to know the
   *  threshold to decide between presenting a finding and a guess. */
  confident: boolean;
  is_fallback: boolean;
  explanation: string;
  source: string;
  reconnect_required: boolean;
  /** A COUNT. The titles are never sent. */
  events_seen?: number;
};

export async function calendarStatus(): Promise<CalendarStatus> {
  return json(await authedFetch(`${API_BASE}/calendar/status`, { cache: "no-store" }));
}

export async function calendarAuthorizeUrl(): Promise<{ authorize_url: string; scopes: string[] }> {
  const res = await authedFetch(`${API_BASE}/calendar/connect`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(
      res.status === 503
        ? "Calendar isn't configured on this deployment (no Google client id/secret)."
        : `HTTP ${res.status}`,
    );
  }
  return (await res.json()) as { authorize_url: string; scopes: string[] };
}

export async function calendarToday(): Promise<CalendarToday> {
  return json(await authedFetch(`${API_BASE}/calendar/today`, { cache: "no-store" }));
}

export async function calendarDisconnect(): Promise<{
  disconnected: boolean;
  revoked_at_provider: boolean;
}> {
  const res = await authedFetch(`${API_BASE}/calendar`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as { disconnected: boolean; revoked_at_provider: boolean };
}

export type TodaysLook = {
  outfits: ChatOutfit[];
  context?: Record<string, unknown> | null;
  notes?: string[];
  /** `requested` | `calendar` | `default`. A user looking at an outfit they
   *  did not ask for is entitled to know what it was dressed for. */
  occasion_source?: string | null;
  /** The classifier's own line — names the RULE PHRASE it matched, never the
   *  event title. The calendar panel promises titles are never sent here. */
  occasion_reason?: string | null;
  calendar_events_seen?: number | null;
};

/** Suggestions with NO occasion named: the server works it out from today's
 *  calendar, and falls back to casual when there is nothing to go on. */
export async function todaysLook(limit = 4): Promise<TodaysLook> {
  return json(
    await authedFetch(`${API_BASE}/suggestions?limit=${limit}`, { cache: "no-store" }),
  );
}

// ------------------------------------------------------------------ ops
//
// ADMIN ONLY. These serve CROSS-TENANT aggregates through SECURITY DEFINER
// functions — the whole deployment's ingest funnel, latency, DLQ depth and
// model spend. They answered for any logged-in account until migration 0019
// added `users.is_admin` and a 403.

export type OpsAlert = {
  alert: string;
  firing: boolean;
  [k: string]: unknown;
};

export async function opsAlerts(): Promise<{ firing: number; alerts: OpsAlert[] }> {
  return json(await authedFetch(`${API_BASE}/ops/alerts`, { cache: "no-store" }));
}

export async function opsDashboards(): Promise<Record<string, unknown>> {
  return json(await authedFetch(`${API_BASE}/ops/dashboards`, { cache: "no-store" }));
}

export async function opsRerank(): Promise<{ checks: OpsAlert[] }> {
  return json(await authedFetch(`${API_BASE}/ops/rerank`, { cache: "no-store" }));
}

// ------------------------------------------------------------------ push
//
// The SERVER half has existed since Phase 8 and is scheduled: `hourly_digest`
// runs every hour and sends only to tenants for whom it is currently 07:00
// where they are. What was missing was a device to send to — nothing in the
// product ever called `POST /push/devices`.

export type PushDevice = {
  id: string;
  platform: string;
  timezone: string | null;
  enabled?: boolean;
  created_at?: string;
};

export async function listPushDevices(): Promise<{ devices: PushDevice[] }> {
  return json(await authedFetch(`${API_BASE}/push/devices`, { cache: "no-store" }));
}

export async function registerPushDevice(
  token: string,
  timezone: string,
): Promise<{ device_id: string }> {
  const res = await authedFetch(`${API_BASE}/push/devices`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, platform: "web", timezone }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${(await res.text()).slice(0, 120)}`);
  return (await res.json()) as { device_id: string };
}

export async function disablePushDevices(): Promise<void> {
  const res = await authedFetch(`${API_BASE}/push/devices`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

// ---------------------------------------------------------------- avatar

export async function getAvatar(): Promise<{ avatar_url: string | null }> {
  return json(await authedFetch(`${API_BASE}/me/avatar`, { cache: "no-store" }));
}

export async function uploadAvatar(file: File): Promise<string> {
  const pre = await json<PresignResponse>(
    await authedFetch(`${API_BASE}/me/avatar/presign`, { method: "POST" }),
  );
  await uploadToStorage(pre, file);
  // Committed as a SECOND call, so a pointer is only stored once the bytes
  // are really there — the server re-checks with a HEAD before saving it.
  // Otherwise a failed upload leaves the profile pointing at a 404 and the
  // avatar shows broken forever.
  const res = await authedFetch(`${API_BASE}/me/avatar`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: pre.key }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${(await res.text()).slice(0, 120)}`);
  return ((await res.json()) as { avatar_url: string }).avatar_url;
}

export async function clearAvatar(): Promise<void> {
  const res = await authedFetch(`${API_BASE}/me/avatar`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

// ------------------------------------------------------------------ shop
//
// Answers ONE question: your wardrobe cannot dress this occasion, what single
// item would fix it? Not a product feed — see routers/shop.py.

export type ShopProduct = {
  id: string;
  merchant: string;
  title: string;
  brand: string | null;
  slot: string;
  subcategory: string | null;
  primary_colour: string | null;
  dress_code: string | null;
  price_minor: number | null;
  currency: string | null;
  url: string;
  image_url: string | null;
};

export type ShopGap = {
  slot: string;
  /** `blocking` = no outfit exists at all. `incomplete` = outfits are shown
   *  but missing this piece. Different urgency, different sentence. */
  severity: string;
  reason: string;
  products: ShopProduct[];
};

export type ShopGaps = {
  occasion: string;
  gaps: ShopGap[];
  /** Rendered VERBATIM. Paid-link disclosure is a legal obligation, and
   *  returning it as data means a new surface cannot forget it. */
  affiliate: string;
  catalogue_empty: boolean;
  /** True when a purchase through these links is reported by the merchant
   *  and added to the wardrobe without the user saying so. */
  auto_add: boolean;
};

export async function shopGaps(occasion: string): Promise<ShopGaps> {
  return json(
    await authedFetch(`${API_BASE}/shop/gaps?occasion=${encodeURIComponent(occasion)}`, {
      cache: "no-store",
    }),
  );
}

/** Records the click, then the caller opens the url it already has.
 *  Deliberately not a redirect: our server must not sit in the path of an
 *  outbound click, or an outage here becomes a broken link to the merchant. */
export async function recordShopClick(productId: string): Promise<void> {
  await authedFetch(`${API_BASE}/shop/click/${productId}`, { method: "POST" }).catch(
    () => undefined,
  );
}

export type OwnedProduct = {
  garment_id: string;
  created: boolean;
  has_image: boolean;
  note: string;
};

/** "I bought this" -> the product becomes a garment.
 *
 * Deliberately NOT fired by the click itself. A click is not a purchase, and
 * filling a wardrobe with things the user merely looked at would make the
 * whole wardrobe untrustworthy — every recommendation is built from it.
 */
export async function ownProduct(productId: string): Promise<OwnedProduct> {
  return json(
    await authedFetch(`${API_BASE}/shop/own/${productId}`, { method: "POST" }),
  );
}

export async function listGarments(): Promise<Garment[]> {
  return json<Garment[]>(
    await authedFetch(`${API_BASE}/garments`, { cache: "no-store" }),
  );
}

export type RemoveResult = {
  garment_id: string;
  removed: boolean;
  already_removed: boolean;
  outfits_pruned?: number;
  /** The row is retired; the photograph is NOT deleted. Full erasure is
   *  DELETE /me. The UI must not claim more than this. */
  photo_retained: boolean;
};

export async function removeGarment(id: string): Promise<RemoveResult> {
  const res = await authedFetch(`${API_BASE}/garments/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as RemoveResult;
}

export async function presign(contentType: string): Promise<PresignResponse> {
  return json<PresignResponse>(
    await authedFetch(`${API_BASE}/uploads/presign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    await authedFetch(`${API_BASE}/garments/ingest`, {
      method: "POST",
      headers: {
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
      const resp = await authedFetch(`${API_BASE}/jobs/${jobId}/events`, { signal: controller.signal,
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
    await authedFetch(`${API_BASE}/garments/${id}/detail`, { cache: "no-store",
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
    await authedFetch(`${API_BASE}/garments/${garmentId}/fields`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
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
    await authedFetch(`${API_BASE}/ops/correction-rate`, { cache: "no-store",
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
    await authedFetch(`${API_BASE}/garments/search?${params}`, { cache: "no-store",
    }),
  );
}

export async function facets(): Promise<Facets> {
  return json(
    await authedFetch(`${API_BASE}/wardrobe/facets`, { cache: "no-store",
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

export type WornToday = {
  date: string;
  items: {
    id: string;
    slot: string | null;
    subcategory: string | null;
    primary_colour: string | null;
    cutout_url: string | null;
    note: string | null;
  }[];
  /** Stated by the server. The home screen branches on "did they dress
   *  today", which is not the same question as "how many garments". */
  wore_something: boolean;
};

export async function wornToday(): Promise<WornToday> {
  return json(
    await authedFetch(`${API_BASE}/wardrobe/worn-on`, { cache: "no-store" }),
  );
}

/** Undo a wear. The server keys it by (garment, date), so this needs both.
 *
 *  Marking something worn by mistake was previously permanent from the UI:
 *  the endpoint existed and no client function called it, so the only way out
 *  was the database. A log you cannot correct stops being a record and becomes
 *  a nuisance -- and this one feeds cost-per-wear and repeat-avoidance, so a
 *  wrong entry quietly skews suggestions too.
 */
export async function unlogWear(id: string, wornOn: string): Promise<void> {
  const res = await authedFetch(`${API_BASE}/garments/${id}/wear/${wornOn}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

export async function logWear(id: string): Promise<WearResponse> {
  return json(
    await authedFetch(`${API_BASE}/garments/${id}/wear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }),
  );
}

export async function setLaundry(id: string, needsWash: boolean): Promise<void> {
  await json(
    await authedFetch(`${API_BASE}/garments/${id}/laundry`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
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
    await authedFetch(`${API_BASE}/wardrobe/most-worn?limit=${limit}`, { cache: "no-store",
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
    await authedFetch(`${API_BASE}/wardrobe/duplicates`, { cache: "no-store",
    }),
  );
}

export async function resolveDuplicate(
  id: string,
  resolution: "different" | "same",
): Promise<void> {
  await json(
    await authedFetch(`${API_BASE}/garments/${id}/duplicate-resolution`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
  // The model that tagged THIS row, from `model_calls`. null = tagged before
  // the ledger existed, or a cache hit that skipped it.
  tagged_by: string | null;
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
  // The models that actually tagged the rows ON THIS PAGE, most-common first.
  // A page-level banner cannot answer a per-row question ("which model made
  // this tag"), but it can answer this one, and the per-row label carries the
  // rest. Before this, the banner asserted every row came from the last call's
  // model, which was wrong about four of nine rows on a real account.
  tagging_models_on_page: { model: string; garments: number }[];
  tagging_mixed_on_page: boolean;
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
    await authedFetch(`${API_BASE}/garments/eval?${p}`, { cache: "no-store",
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
    await authedFetch(`${API_BASE}/me/preferences`, { cache: "no-store" }),
  );
}

export async function addPreference(
  kind: PreferenceFact["kind"],
  fieldName: string,
  fieldValue: string,
): Promise<PreferenceFact> {
  return json(
    await authedFetch(`${API_BASE}/me/preferences`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, field_name: fieldName, field_value: fieldValue }),
    }),
  );
}

export async function deletePreference(id: string): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/me/preferences/${id}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
}

// ---------------------------------------------------------------- chat

export type ChatGarment = {
  id: string;
  slot: string | null;
  subcategory: string | null;
  primary_colour: string | null;
  cutout_url: string | null;
  needs_review?: boolean;
};

export type ChatOutfit = {
  garments: ChatGarment[];
  score: number;
  /** 1-based preference order, stated by the server. Do NOT recompute from
   *  array position: screens that filter or re-sort would renumber the
   *  ranking into nonsense, and only the server knows the real order. */
  rank?: number | null;
  /** Where the SCORER put it, before the bandit's exploration reshuffled the
   *  list. When this differs from `rank`, the card is being shown higher (or
   *  lower) than predicted on purpose, and the UI says so rather than
   *  claiming the scorer's endorsement. */
  predicted_rank?: number | null;
  // The outfit's identity. Required to request a try-on or a board — without
  // it the Try On button has nothing to ask for.
  garment_set_hash?: string | null;
  rationale?: string | null;
  informative_weight?: number | null;
  score_breakdown?: Record<string, unknown> | null;
};

export type ChatReply = {
  reply: string;
  understood: {
    occasion: string;
    matched: string | null;
    feels_like_c: number;
    weather_stated: boolean;
  } | null;
  outfits: ChatOutfit[];
  examples?: string[];
  notes?: string[];
  ranking_source?: string | null;
  served_from?: string | null;
  explored_slots?: number | null;
  context?: Record<string, unknown> | null;
  needs_clarification: boolean;
};

export async function askStylist(message: string, limit = 4): Promise<ChatReply> {
  const res = await authedFetch(`${API_BASE}/chat?limit=${limit}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    // The body carries FastAPI's `detail`, which is the only thing that
    // distinguishes "your session expired" from "the wardrobe is empty".
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new Error(detail);
  }
  return (await res.json()) as ChatReply;
}

// ---------------------------------------------------------------- try-on

export type TryOnResult = {
  garment_set_hash: string;
  rendered: boolean;
  reason: string | null;
  queued?: boolean;
  tryon_url?: string | null;
  board_url?: string | null;
  /** Slots the render did NOT cover. No try-on provider renders footwear, so
   *  `feet` is here on essentially every render and the shoes in the image are
   *  the ones from the user's own body photo. */
  skipped_slots?: string[];
  board_endpoint: string;
};

/** Ask for a render of one outfit. ALWAYS 200 by design — see the router:
 *  "works or degrades to a board, never errors". So a falsy `rendered` is a
 *  normal answer carrying a `reason`, not a failure to handle. */
export async function requestTryOn(garmentSetHash: string): Promise<TryOnResult> {
  const res = await authedFetch(`${API_BASE}/outfits/${garmentSetHash}/tryon`, { method: "POST" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as TryOnResult;
}

export type OutfitBoard = { board_url: string | null; garment_set_hash: string };

export async function outfitBoard(garmentSetHash: string): Promise<OutfitBoard> {
  const res = await authedFetch(`${API_BASE}/outfits/${garmentSetHash}/board`, { });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as OutfitBoard;
}

// -------------------------------------------------- feedback / saved looks

export type FeedbackResult = {
  event_id: string;
  kind: string;
  style_vector_events: number;
  style_vector_moved: boolean;
  /** Which Thompson arm moved, or null when the reaction carried no evidence. */
  bandit_arm?: string | null;
};

/** A VERDICT on a suggestion: `like` or `dislike`.
 *
 *  This is the signal the recommender was missing entirely. The only kind the
 *  UI could previously send was `saved`, which moves the style vector at half
 *  weight and moves the Thompson posteriors NOT AT ALL (`apply_feedback`
 *  treats saving as intent, not evidence). So `bandit_arm` was empty across
 *  the whole database: not a broken loop, a loop with no way to be fed.
 */
export async function rateOutfit(
  garmentIds: string[],
  kind: "like" | "dislike",
  occasion = "casual_outing",
): Promise<FeedbackResult> {
  return json(
    await authedFetch(`${API_BASE}/outfits/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        garment_ids: garmentIds,
        occasion,
        kind,
        was_suggested: true,
      }),
    }),
  );
}

/** "I WORE THIS" — the strongest signal the recommender can receive, and the
 *  one it was never given.
 *
 *  Two gaps closed by one call:
 *
 *  1. LEARNING. `apply_feedback` counts `worn` as a success for the Thompson
 *     arm and `style.POSITIVE` weights it 1.0 — the same as a like, because
 *     actually wearing an outfit is a stronger endorsement than tapping a
 *     thumb. Nothing ever sent it: `wear.py` writes `wear_log` and no
 *     `outfit_feedback` row at all.
 *
 *  2. MEASUREMENT. `GET /me/wear-through` computes suggested -> actually worn
 *     from `outfit_feedback WHERE kind = 'worn'`, and the plan calls that the
 *     only quality metric that matters. With nothing writing those rows the
 *     numerator was structurally zero, so the metric could only ever report
 *     0% or "unmeasured" no matter how good the suggestions were.
 *
 *  The feedback event is sent FIRST and awaited: it is what both the learning
 *  and the metric depend on. The per-garment wear rows follow and are allowed
 *  to fail individually — they drive cost-per-wear and repeat-avoidance, which
 *  degrade gracefully, unlike the event above.
 */
export async function markOutfitWorn(
  garmentIds: string[],
  occasion = "casual_outing",
): Promise<FeedbackResult> {
  const result = await json<FeedbackResult>(
    await authedFetch(`${API_BASE}/outfits/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        garment_ids: garmentIds,
        occasion,
        kind: "worn",
        was_suggested: true,
      }),
    }),
  );
  await Promise.allSettled(garmentIds.map((id) => logWear(id)));
  return result;
}

/** Save a look. `saved` is a real `feedback_kind`, so the heart on a card
 *  writes an event the Saved Looks screen reads back — it is not decorative.
 *
 *  Deliberately NOT `like`: saving is intent, liking is a verdict, and the
 *  style vector weights them differently (0.5 vs 1.0). Conflating them would
 *  teach the recommender that bookmarking something is endorsement. */
export async function saveOutfit(garmentIds: string[], occasion = "casual_outing") {
  const res = await authedFetch(`${API_BASE}/outfits/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      garment_ids: garmentIds,
      occasion,
      kind: "saved",
      was_suggested: true,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

// ------------------------------------------------------------ body photos

export type BodyPhoto = {
  id: string;
  consented_at: string;
  revoked_at: string | null;
  deleted_from_storage: boolean;
};

export type BodyPhotoPresign = {
  upload_id: string;
  key: string;
  url: string;
  fields: Record<string, string>;
  expires_at: string;
  notice: string;
  sent_to_third_party: string | null;
};

/** Where the upload will go, plus the CONSENT NOTICE to show before it does.
 *  The notice names the third party the photo would be sent to when a provider
 *  is configured — consent to storage is not consent to transmission, and the
 *  UI must show the server's wording rather than paraphrase it. */
export async function presignBodyPhoto(): Promise<BodyPhotoPresign> {
  return json<BodyPhotoPresign>(
    await authedFetch(`${API_BASE}/me/body-photos/presign`, { method: "POST" }),
  );
}

export async function uploadBodyPhoto(p: BodyPhotoPresign, file: File): Promise<void> {
  const form = new FormData();
  for (const [k, v] of Object.entries(p.fields)) form.append(k, v);
  form.append("file", file);
  const res = await fetch(p.url, { method: "POST", body: form });
  if (!res.ok) throw new Error(`upload failed: HTTP ${res.status}`);
}

/** Record the photo AND the consent together.
 *  `consent_to_virtual_tryon` is explicit on purpose — the server rejects
 *  false, because "they uploaded it so they must have agreed" is the reasoning
 *  that makes consent a formality. */
export async function recordBodyPhoto(p: BodyPhotoPresign): Promise<{ body_photo_id: string }> {
  return json<{ body_photo_id: string }>(
    await authedFetch(`${API_BASE}/me/body-photos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: p.upload_id,
        key: p.key,
        consent_to_virtual_tryon: true,
      }),
    }),
  );
}

export async function listBodyPhotos(): Promise<{ photos: BodyPhoto[]; active: number }> {
  return json<{ photos: BodyPhoto[]; active: number }>(
    await authedFetch(`${API_BASE}/me/body-photos`),
  );
}

/** Revoke consent and delete the photo. Does NOT touch the account — §C5
 *  requires the two be separable, because withdrawing consent for one feature
 *  must not cost you the product. */
export async function revokeBodyPhotos(): Promise<unknown> {
  return json<unknown>(await authedFetch(`${API_BASE}/me/body-photos`, { method: "DELETE" }));
}

// ------------------------------------------------------------ dresses as

export async function getDressesAs(): Promise<{ dresses_as: DressesAs | null; asked: boolean }> {
  const res = await authedFetch(`${API_BASE}/me/dresses-as`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as { dresses_as: DressesAs | null; asked: boolean };
}

export async function setDressesAs(value: DressesAs): Promise<void> {
  const res = await authedFetch(`${API_BASE}/me/dresses-as`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dresses_as: value }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${(await res.text()).slice(0, 120)}`);
}
