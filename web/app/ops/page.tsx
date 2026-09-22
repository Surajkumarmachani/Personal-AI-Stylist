"use client";

/** Operations, for admins.
 *
 * WHY THIS SCREEN IS GATED AND SAYS SO
 * These numbers are CROSS-TENANT: the whole deployment's ingest funnel,
 * latency percentiles, DLQ depth and model spend, read through SECURITY
 * DEFINER functions that deliberately bypass RLS. Until migration 0019 any
 * registered account could fetch all of it — `routers/ops.py` had said since
 * Phase 5 that it "belongs behind an admin authorisation boundary rather than
 * a user token", and that boundary had never been built.
 *
 * A non-admin gets a plain 403 here rather than a redirect, because the route
 * is in the public OpenAPI document and pretending it does not exist would be
 * theatre.
 */

import { useEffect, useState } from "react";
import Shell from "../Shell";
import SignIn from "../SignIn";
import { restoreSession } from "../session";
import { opsAlerts, opsDashboards, opsRerank, type OpsAlert } from "@/lib/api";
import "../ui.css";

function Alerts({ title, alerts }: { title: string; alerts: OpsAlert[] }) {
  return (
    <div className="ui-panel" style={{ marginBottom: 18 }}>
      <h2 className="ui-h3">{title}</h2>
      {alerts.length === 0 ? (
        <p className="ui-sub">Nothing reported.</p>
      ) : (
        alerts.map((a) => (
          <div
            key={a.alert}
            style={{
              display: "flex",
              gap: 10,
              alignItems: "baseline",
              padding: "8px 0",
              borderTop: "1px solid var(--line)",
            }}
          >
            <span
              aria-hidden="true"
              style={{ color: a.firing ? "var(--bad, #b4232a)" : "var(--ok)" }}
            >
              {a.firing ? "●" : "○"}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <b style={{ fontSize: 13 }}>{a.alert.replace(/_/g, " ")}</b>
              <p className="ui-sub" style={{ margin: 0, wordBreak: "break-word" }}>
                {Object.entries(a)
                  .filter(([k]) => k !== "alert" && k !== "firing")
                  .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`)
                  .join(" · ")}
              </p>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

export default function OpsPage() {
  const [email, setEmail] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [alerts, setAlerts] = useState<OpsAlert[] | null>(null);
  const [rerank, setRerank] = useState<OpsAlert[] | null>(null);
  const [dash, setDash] = useState<Record<string, unknown> | null>(null);
  const [denied, setDenied] = useState(false);

  useEffect(() => {
    void restoreSession()
      .then(setEmail)
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!email) return;
    opsAlerts()
      .then((r) => setAlerts(r.alerts))
      .catch((e) => {
        if (String(e).includes("403")) setDenied(true);
      });
    opsRerank().then((r) => setRerank(r.checks)).catch(() => undefined);
    opsDashboards().then(setDash).catch(() => undefined);
  }, [email]);

  if (checking) return <div className="ui" style={{ display: "block" }} />;
  if (!email) return <SignIn onDone={setEmail} />;

  if (denied) {
    return (
      <Shell email={email} back>
        <div className="ui-panel">
          <h2 className="ui-h3">Admin only</h2>
          <p className="ui-sub">
            These figures cover the whole deployment, not just your account, so they are
            behind an admin flag. It is granted with SQL —{" "}
            <code>scripts/grant_admin.py</code> — never through the API.
          </p>
        </div>
      </Shell>
    );
  }

  return (
    <Shell email={email} back>
      <h1 style={{ margin: "0 0 6px", fontSize: 26, fontWeight: 640 }}>Operations</h1>
      <p className="ui-sub" style={{ marginBottom: 20 }}>
        Deployment-wide, across every account.
      </p>

      {alerts ? <Alerts title="Alerts" alerts={alerts} /> : <p className="ui-sub">Loading…</p>}
      {rerank ? <Alerts title="Reranker" alerts={rerank} /> : null}

      {dash ? (
        <div className="ui-panel">
          <h2 className="ui-h3">Dashboards</h2>
          {/* Rendered as JSON on purpose. These shapes change as the ops
              endpoints grow, and a hand-drawn table would silently drop a new
              field — worse than showing the raw answer. */}
          <pre
            className="ui-sub"
            style={{ overflowX: "auto", margin: 0, fontSize: 11.5, lineHeight: 1.5 }}
          >
            {JSON.stringify(dash, null, 2)}
          </pre>
        </div>
      ) : null}
    </Shell>
  );
}
