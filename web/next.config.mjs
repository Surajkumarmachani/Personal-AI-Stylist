import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

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
  // The API base is read at runtime, not baked in at build: the same image
  // has to work in local compose, dev and prod, and rebuilding to change a
  // hostname is how you end up unable to roll back.
  env: {
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080",
  },
};
export default nextConfig;
