"use client";

/** Session restore for the product screens.
 *
 * THE BUG THIS REPLACED. The email used to live in sessionStorage while the
 * token lived only in memory, so after a page load `recallEmail()` returned an
 * address, every screen concluded it was signed in, and the first API call came
 * back `401 missing bearer token`. The UI looked authenticated over a session
 * that did not exist.
 *
 * The gate now hangs on `hasSession()` — whether a token can actually be
 * obtained — and the email is only a display name. Nothing decides
 * authentication from it.
 */

import { hasSession, refreshSession } from "@/lib/api";

const KEY = "stylist.email";

export function rememberEmail(email: string): void {
  try {
    sessionStorage.setItem(KEY, email);
  } catch {
    /* private mode: the app still works, it just shows "account" instead */
  }
}

export function recallEmail(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function forgetEmail(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* nothing to do */
  }
}

/** Restore a usable session, or report that there is none.
 *
 * Returns the display email on success. Callers render the sign-in screen on
 * null — and because this resolves only after the refresh round trip, screens
 * must treat "still resolving" as distinct from "signed out", or they flash
 * the sign-in form at every authenticated user on every load.
 */
export async function restoreSession(): Promise<string | null> {
  if (!hasSession()) return null;
  const ok = await refreshSession();
  if (!ok) {
    forgetEmail();
    return null;
  }
  return recallEmail() ?? "account";
}
