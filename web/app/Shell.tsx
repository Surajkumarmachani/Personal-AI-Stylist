"use client";

/** The app shell: sidebar + top bar, shared by every product screen.
 *
 * One component rather than a layout.tsx, because the access token is
 * in-memory (see lib/api.ts) and each screen therefore has to be able to gate
 * on sign-in itself — a layout cannot do that without lifting auth state it
 * has no place holding.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";

const NAV = [
  { href: "/", label: "Home", ic: "⌂" },
  { href: "/explore", label: "Explore", ic: "◎" },
  { href: "/wardrobe", label: "Wardrobe", ic: "▣" },
  { href: "/tryon", label: "Try On", ic: "☰" },
  { href: "/stylist", label: "AI Stylist", ic: "✦" },
  { href: "/occasions", label: "Occasions", ic: "◷" },
  { href: "/profile", label: "Profile", ic: "☺" },
];

export default function Shell({
  email,
  children,
  back = false,
  title,
}: {
  email?: string | null;
  children: ReactNode;
  back?: boolean;
  title?: ReactNode;
}) {
  const path = usePathname();
  const router = useRouter();
  return (
    <div className="ui">
      <aside className="ui-side">
        <div className="ui-logo">
          <i aria-hidden="true" />
          AI Stylist
        </div>
        {NAV.map((n) => (
          <Link key={n.href} href={n.href} className={path === n.href ? "on" : undefined}>
            <span className="ic" aria-hidden="true">{n.ic}</span>
            {n.label}
          </Link>
        ))}

        {/* NOT A BUTTON. There is no billing, no plan and no payment table in
            this system, so an Upgrade control would be the one thing on these
            screens that lies about what it does. It says so instead. */}
        <div className="ui-pro">
          <b>Pro plan</b>
          Subscriptions aren&apos;t wired up — no billing exists in this build yet.
        </div>
      </aside>

      <main className="ui-main">
        <header className="ui-top">
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
            {back ? (
              <button className="ui-back" onClick={() => router.back()} aria-label="Back">
                ←
              </button>
            ) : null}
            {title}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ color: "var(--muted)", fontSize: 15 }} aria-hidden="true">
              ◔
            </span>
            <span className="ui-avatar">{email ? email[0]!.toUpperCase() : "·"}</span>
          </div>
        </header>
        <div className="ui-body">{children}</div>
      </main>
    </div>
  );
}
