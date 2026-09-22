"use client";

/** Connect Google Calendar, so "what's on today" picks the occasion.
 *
 * WHY THIS IS THE FEATURE WORTH HAVING
 * Every other route into a suggestion asks the user to name the occasion.
 * This one already knows: a calendar with "Standup", "Client review", "Rahul's
 * sangeet" in it answers the question the rest of the app keeps asking.
 *
 * WHAT IS AND IS NOT SENT BACK
 * `/calendar/today` returns a CLASSIFICATION and a COUNT of events — never
 * the titles, and the backend never persists them. That is a deliberate
 * design decision worth stating on screen, because "let an app read my
 * calendar" is a large ask and the honest answer to "what do you keep?" is
 * "nothing". Read-only scope, and disconnect revokes at Google.
 *
 * THE OUTCOME ARRIVES IN THE QUERY STRING
 * Google performs a top-level navigation to the API's callback, which now
 * redirects here with `?calendar=connected|failed`. The app cannot read that
 * response any other way — it was never a fetch this client made.
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  calendarAuthorizeUrl,
  calendarDisconnect,
  calendarStatus,
  calendarToday,
  type CalendarStatus,
  type CalendarToday,
} from "@/lib/api";

export default function CalendarConnect() {
  const [status, setStatus] = useState<CalendarStatus | null>(null);
  const [today, setToday] = useState<CalendarToday | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const router = useRouter();
  const params = useSearchParams();

  const refresh = useCallback(() => {
    calendarStatus()
      .then((s) => {
        setStatus(s);
        if (s.connected) calendarToday().then(setToday).catch(() => undefined);
        else setToday(null);
      })
      .catch(() => undefined);
  }, []);

  useEffect(refresh, [refresh]);

  useEffect(() => {
    const outcome = params.get("calendar");
    if (!outcome) return;
    setNote(
      outcome === "connected"
        ? "Calendar connected."
        : "Google did not complete the connection. Nothing was saved.",
    );
    // Clear the query so a refresh does not replay the banner.
    router.replace("/profile");
    refresh();
  }, [params, router, refresh]);

  async function connect() {
    setBusy(true);
    setErr(null);
    try {
      const { authorize_url } = await calendarAuthorizeUrl();
      // A full navigation, not a popup: Google blocks its consent screen in
      // many embedded contexts, and the callback redirects back here anyway.
      window.location.href = authorize_url;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    setErr(null);
    try {
      const res = await calendarDisconnect();
      setNote(
        res.revoked_at_provider
          ? "Disconnected, and the token was revoked at Google."
          : "Disconnected here. Google did not confirm the revoke — check " +
            "myaccount.google.com/permissions if you want to be certain.",
      );
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 18 }}>
      <h2 className="ui-h3">Your calendar</h2>

      {status?.connected ? (
        <>
          <p className="ui-sub" style={{ marginBottom: 10 }}>
            Connected as <b style={{ color: "var(--ink)" }}>{status.account_email}</b>. Read-only,
            and only today&apos;s events are ever looked at.
          </p>

          {today ? (
            <div
              style={{
                border: "1px solid var(--line)",
                borderRadius: 10,
                padding: "10px 12px",
                marginBottom: 10,
              }}
            >
              <p className="ui-sub" style={{ margin: 0 }}>
                {today.events_seen === 0 ? (
                  "Nothing in your calendar today."
                ) : (
                  <>
                    <b style={{ color: "var(--ink)" }}>
                      {today.occasion.replace(/_/g, " ")}
                    </b>{" "}
                    — {today.explanation}
                    {today.confident ? "" : " (a guess, not a finding)"}
                  </>
                )}
              </p>
              {today.events_seen ? (
                <button
                  className="ui-btn primary"
                  style={{ marginTop: 10 }}
                  onClick={() => router.push(`/explore?o=${encodeURIComponent(today.occasion)}`)}
                >
                  Dress for it
                </button>
              ) : null}
            </div>
          ) : null}

          <button className="ui-btn" onClick={() => void disconnect()} disabled={busy}>
            {busy ? "…" : "Disconnect"}
          </button>
        </>
      ) : (
        <>
          <p className="ui-sub" style={{ marginBottom: 10 }}>
            Let the stylist see what&apos;s on today, so it can dress you for it without being
            asked. <b style={{ color: "var(--ink)" }}>Read-only</b>, today&apos;s events only, and
            the titles are never sent to this app or stored — only a count and the occasion they
            add up to. Disconnecting revokes the access at Google.
          </p>
          <button className="ui-btn primary" onClick={() => void connect()} disabled={busy}>
            {busy ? "Opening Google…" : "Connect Google Calendar"}
          </button>
        </>
      )}

      {note ? <p className="ui-sub" style={{ color: "var(--ok)", marginTop: 10 }}>{note}</p> : null}
      {err ? <p className="ui-err" style={{ marginTop: 10 }}>{err}</p> : null}
    </div>
  );
}
