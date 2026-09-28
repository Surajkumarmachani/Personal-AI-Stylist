import type { MetadataRoute } from "next";

/** Web app manifest: what makes the site installable as a desktop app.
 *
 * Chrome and Edge offer "Install" once this is served; the result opens in
 * its own window with its own dock/taskbar icon, and Firebase push keeps
 * working because it is still the same origin and the same service worker.
 * That is the whole desktop story until a native wrapper is needed.
 *
 * Colours are the tokens in globals.css (--bg, --accent), repeated as literals
 * because a manifest is JSON and cannot read CSS variables.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Personal AI Stylist",
    short_name: "Stylist",
    description: "Your wardrobe, and what to wear from it.",
    id: "/",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#fbfaf8",
    theme_color: "#7b1f2b",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
