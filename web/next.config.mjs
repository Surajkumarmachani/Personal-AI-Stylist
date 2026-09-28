import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

/** Read the REPO-ROOT `.env`, which is where this project keeps configuration.
 *
 * Next only loads `.env` files from its own directory (`web/`), and compose
 * loads the one at the repo root. Duplicating five keys into a second file is
 * how the two silently diverge — the Firebase config would be right in the
 * container and empty in the browser, which presents as "push just does not
 * work" with nothing in any log.
 *
 * Parsed by hand rather than adding `dotenv`: this needs to handle
 * `KEY=value`, comments and blank lines, and nothing else.
 */
function readRootEnv() {
  try {
    const text = readFileSync(join(here, "..", ".env"), "utf8");
    return Object.fromEntries(
      text
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line && !line.startsWith("#") && line.includes("="))
        .map((line) => {
          const i = line.indexOf("=");
          return [line.slice(0, i).trim(), line.slice(i + 1).trim()];
        }),
    );
  } catch {
    // No root .env (a fresh clone, or CI): every value falls back to "" and
    // the push UI reports itself unconfigured rather than throwing.
    return {};
  }
}

const rootEnv = readRootEnv();

/** Fail a Vercel PRODUCTION build that has no API base.
 *
 * Without it the bundle bakes in the localhost default below and deploys
 * green: every page loads, and every request dies in the browser as "Failed
 * to fetch" with nothing in any log. A build error names the missing variable.
 * Preview builds are allowed through so a branch without a backend still gets
 * a URL to look at.
 */
if (process.env.VERCEL_ENV === "production" && !process.env.NEXT_PUBLIC_API_BASE) {
  throw new Error(
    "NEXT_PUBLIC_API_BASE is not set for this Vercel production build. " +
      "Set it to the Cloud Run API URL (infra/gcp/deploy.sh prints it).",
  );
}

/** Headers for every response. Deliberately no Content-Security-Policy yet:
 * the app loads Firebase from gstatic and images from presigned bucket URLs,
 * and a CSP that is wrong breaks the app in ways that only show in the
 * browser console. These four cannot break anything the app does.
 */
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Nothing embeds this app; refusing frames rules out clickjacking.
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Photos are picked with <input type=file>, the city is typed: none of
  // these device APIs is used, so none is granted.
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];


/** @type {import('next').NextConfig} */
const nextConfig = {
  // Pin the workspace root to THIS directory instead of letting it be
  // inferred. Left to inference the dev watcher walked up to $HOME and tried
  // to stat ~/OrbStack — an NFS mount from OrbStack (which also hosts this
  // project's Docker stack) — and every scan failed with
  //
  //   Watchpack Error (initial scan): Error: ETIMEDOUT: connection timed out,
  //   lstat '/Users/surajkumar/OrbStack'
  //
  // The dev server still serves, but the watcher is degraded, so hot reload
  // becomes slow or unreliable. Pinning the root keeps the scan inside web/.
  turbopack: {
    root: here,
  },
  // Same reason, for the production trace: without it the tracer can wander
  // out of the project and either time out or pull in unrelated files.
  outputFileTracingRoot: here,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  // The API base is read at runtime, not baked in at build: the same image
  // has to work in local compose, dev and prod, and rebuilding to change a
  // hostname is how you end up unable to roll back.
  env: {
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080",
    // Firebase web config, read from the REPO-ROOT .env — see rootEnv above —
    // falling back to the process environment. The fallback is what makes
    // Vercel work: .env is gitignored, so a hosted build has no root file and
    // these would bake in as "" — push silently dead in production.
    // These five are public by design: they ship to every browser that loads
    // the app, which is what lets it register for push. The service account
    // that SENDS notifications is a different credential entirely and lives
    // only in secrets/, mounted read-only into the worker.
    NEXT_PUBLIC_FIREBASE_API_KEY: rootEnv.NEXT_PUBLIC_FIREBASE_API_KEY ?? process.env.NEXT_PUBLIC_FIREBASE_API_KEY ?? "",
    NEXT_PUBLIC_FIREBASE_PROJECT_ID: rootEnv.NEXT_PUBLIC_FIREBASE_PROJECT_ID ?? process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID ?? "",
    NEXT_PUBLIC_FIREBASE_SENDER_ID: rootEnv.NEXT_PUBLIC_FIREBASE_SENDER_ID ?? process.env.NEXT_PUBLIC_FIREBASE_SENDER_ID ?? "",
    NEXT_PUBLIC_FIREBASE_APP_ID: rootEnv.NEXT_PUBLIC_FIREBASE_APP_ID ?? process.env.NEXT_PUBLIC_FIREBASE_APP_ID ?? "",
    NEXT_PUBLIC_FIREBASE_VAPID_KEY: rootEnv.NEXT_PUBLIC_FIREBASE_VAPID_KEY ?? process.env.NEXT_PUBLIC_FIREBASE_VAPID_KEY ?? "",
  },
};
export default nextConfig;
