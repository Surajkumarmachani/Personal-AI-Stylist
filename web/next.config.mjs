/** @type {import('next').NextConfig} */
const nextConfig = {
  // The API base is read at runtime, not baked in at build: the same image
  // has to work in local compose, dev and prod, and rebuilding to change a
  // hostname is how you end up unable to roll back.
  env: {
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080",
  },
};
export default nextConfig;
