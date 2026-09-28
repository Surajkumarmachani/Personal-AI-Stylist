"use client";

/** Add purchases automatically, from forwarded order emails (Phase 15).
 *
 * The user gets a private address. A one-time Gmail filter forwards order
 * emails from Myntra, AJIO, Amazon and others to it, and each purchase appears
 * in the wardrobe with the store's photo and details.
 *
 * SETUP HAS ONE STEP THE APP MUST HELP WITH
 * Gmail will not forward to a new address until it is confirmed, and it sends
 * the confirmation code TO that address, which is ours. So the code is shown
 * here (polled while the panel is open), or the user could never finish.
 *
 * THE LOG IS THE POINT
 * Every forwarded email is listed with what became of it, including "ignored"
 * and "failed". A feature that silently does nothing is indistinguishable from
 * one that is broken.
 */

import { useCallback, useEffect, useState } from "react";
import {
  purchaseInbox,
  rotatePurchaseInbox,
  type PurchaseEmail,
  type PurchaseInbox as Inbox,
} from "@/lib/api";

function outcome(e: PurchaseEmail): string {
  const d = e.detail ?? {};
  switch (e.status) {
    case "added":
      if (d.retired) return `Removed ${d.retired} returned item${d.retired > 1 ? "s" : ""}`;
      return `Added: ${(d.added ?? []).map((a) => a.title).join(", ")}`;
    case "nothing_to_add":
      if (d.already_in_wardrobe?.length) return "Already in your wardrobe";
      return d.dropped?.length ? `Skipped: ${d.dropped.join("; ")}` : "No clothing in this email";
    case "ignored":
      return d.reason ?? "Not from a store we read";
    case "gmail_confirmation":
      return "Gmail forwarding confirmation";
    case "failed":
      return d.error ?? "Could not read this email";
    default:
      return "Reading…";
  }
}

export default function PurchaseInbox() {
  const [inbox, setInbox] = useState<Inbox | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    purchaseInbox()
      .then(setInbox)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh();
    // While setup is unfinished the Gmail code can arrive at any moment, and
    // a freshly forwarded email takes a few seconds to read. Poll gently.
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  if (!inbox) return null;

  function copy(value: string, what: string) {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    });
  }

  return (
    <div className="ui-panel" style={{ marginBottom: 18 }}>
      <h2 className="ui-h3">Add purchases automatically</h2>

      {!inbox.enabled || !inbox.address ? (
        <p className="ui-sub">Not switched on for this server yet.</p>
      ) : (
        <>
          <p className="ui-sub" style={{ marginBottom: 12 }}>
            Forward your order emails from Myntra, AJIO, Amazon, Flipkart and more to your private
            address, and each item appears in your wardrobe with its photo, brand and price. Returns
            and cancellations are removed again.
          </p>

          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <code
              style={{
                padding: "8px 12px",
                borderRadius: 8,
                background: "var(--bg)",
                border: "1px solid var(--line)",
                fontSize: 13.5,
                wordBreak: "break-all",
              }}
            >
              {inbox.address}
            </code>
            <button className="ui-btn" onClick={() => copy(inbox.address ?? "", "address")}>
              {copied === "address" ? "Copied" : "Copy"}
            </button>
          </div>

          {inbox.gmail_confirmation ? (
            <div
              className="ui-panel"
              style={{ marginTop: 14, borderColor: "var(--accent)", background: "var(--bg)" }}
            >
              <p style={{ margin: 0, fontWeight: 600 }}>Gmail sent a confirmation code</p>
              {inbox.gmail_confirmation.code ? (
                <p style={{ margin: "6px 0", fontSize: 20, letterSpacing: 2 }}>
                  {inbox.gmail_confirmation.code}
                </p>
              ) : null}
              <p className="ui-sub" style={{ margin: 0 }}>
                Enter it in Gmail → Settings → Forwarding and POP/IMAP
                {inbox.gmail_confirmation.link ? (
                  <>
                    , or{" "}
                    <a href={inbox.gmail_confirmation.link} target="_blank" rel="noopener noreferrer">
                      confirm with this link
                    </a>
                  </>
                ) : null}
                .
              </p>
            </div>
          ) : null}

          <details style={{ marginTop: 14 }}>
            <summary className="ui-sub" style={{ cursor: "pointer" }}>
              Set it up in Gmail (2 minutes, once)
            </summary>
            <ol className="ui-sub" style={{ paddingLeft: 18, marginTop: 8, lineHeight: 1.7 }}>
              <li>
                Gmail → ⚙ <strong>See all settings</strong> → <strong>Forwarding and POP/IMAP</strong>{" "}
                → <strong>Add a forwarding address</strong> → paste the address above.
              </li>
              <li>Gmail sends a confirmation code. It appears on this page; enter it in Gmail.</li>
              <li>
                Back in Gmail, paste this into the search bar, then click the filter icon →{" "}
                <strong>Create filter</strong> → <strong>Forward it to</strong> your address:
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                  <code style={{ fontSize: 12, wordBreak: "break-all" }}>{inbox.gmail_filter}</code>
                  <button
                    className="ui-btn"
                    onClick={() => copy(inbox.gmail_filter ?? "", "filter")}
                  >
                    {copied === "filter" ? "Copied" : "Copy"}
                  </button>
                </div>
              </li>
              <li>No Gmail filter? Forwarding any order email to the address by hand works too.</li>
            </ol>
          </details>

          {inbox.recent.length ? (
            <div style={{ marginTop: 14 }}>
              <p className="ui-sub" style={{ marginBottom: 6, fontWeight: 600 }}>
                Recent emails
              </p>
              {inbox.recent.slice(0, 8).map((e) => (
                <p key={e.id} className="ui-sub" style={{ margin: "0 0 6px", fontSize: 12.5 }}>
                  <strong>{e.store ?? "Email"}</strong> · {e.subject || "(no subject)"} —{" "}
                  {outcome(e)}
                </p>
              ))}
            </div>
          ) : null}

          <button
            className="ui-btn"
            style={{ marginTop: 12 }}
            disabled={busy}
            onClick={() => {
              setBusy(true);
              rotatePurchaseInbox()
                .then(refresh)
                .finally(() => setBusy(false));
            }}
            title="Retires the current address. Update your Gmail filter afterwards."
          >
            {busy ? "Changing…" : "Get a new address"}
          </button>
        </>
      )}
    </div>
  );
}
