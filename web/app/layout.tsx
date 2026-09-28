import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Wardrobe",
  description: "Personal AI Stylist — wardrobe",
  // Title shown under the icon when installed from Safari (macOS "Add to Dock").
  appleWebApp: { title: "Stylist", capable: true },
};

// The installed window's title bar colour; --accent in globals.css.
export const viewport: Viewport = {
  themeColor: "#7b1f2b",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
