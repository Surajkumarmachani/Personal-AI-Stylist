"use client";

/** What to buy, ONLY when the wardrobe cannot dress the occasion.
 *
 * WHY THIS IS NOT A SHOP FRONT
 * The product's claim, on the same screens, is "every look is built from
 * clothes you own". A feed of things to buy beside that undermines the only
 * thing this app does that a retailer does not.
 *
 * So this renders nothing at all unless the candidate pool for the occasion
 * actually came back short — and it names WHY ("no footwear that suits this
 * occasion") before it names a product. The user arrived here by trying to
 * get dressed, not by being sold to.
 *
 * BUYING THROUGH A LINK ADDS THE GARMENT BY ITSELF when the server says so
 * (`auto_add`): the link carries a reference the merchant reports back with
 * the order. "I bought this" stays, because that report lands hours later and
 * a deployment with no affiliate postback never sends one.
 *
 * DISCLOSURE COMES FROM THE SERVER AND IS RENDERED VERBATIM.
 * Paid-link disclosure is a legal obligation (ASA, FTC), not a footer style
 * choice, so the text is a field on the response rather than a string in this
 * file that a second surface could forget to copy.
 */

import { useEffect, useState } from "react";
import { ownProduct, recordShopClick, shopGaps, type ShopGaps } from "@/lib/api";

function price(minor: number | null, currency: string | null): string {
  if (minor == null) return "";
  const major = Math.round(minor / 100);
  return `${currency === "INR" ? "₹" : ""}${major.toLocaleString("en-IN")}`;
}

export default function FillTheGap({ occasion }: { occasion: string }) {
  const [data, setData] = useState<ShopGaps | null>(null);
  // "I bought this" appears only AFTER the user has been sent to the
  // merchant. Offering it beforehand invites a wardrobe full of things nobody
  // owns, and every recommendation this app makes is built from that wardrobe
  // — so a wrong entry there is not cosmetic, it corrupts the suggestions.
  const [visited, setVisited] = useState<Record<string, boolean>>({});
  const [owned, setOwned] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    shopGaps(occasion)
      .then((r) => live && setData(r))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [occasion]);

  // Nothing missing, or nothing we can honestly offer: render NOTHING. An
  // empty "no gaps!" panel is still a shopping panel on a screen that is not
  // about shopping.
  if (!data || data.gaps.length === 0) return null;

  return (
    <section style={{ marginTop: 30 }}>
      <h2 className="ui-h2">Your wardrobe is short a piece</h2>
      <p className="ui-sub" style={{ marginBottom: 14 }}>
        These are the only things stopping this occasion working. Everything else in the look
        above is already yours.
      </p>

      {data.gaps.map((gap) => (
        <div key={gap.slot} className="ui-panel" style={{ marginBottom: 14 }}>
          <h3 className="ui-h3" style={{ fontSize: 14 }}>
            {gap.severity === "blocking" ? "Needed" : "Would complete it"}:{" "}
            {gap.slot.replace(/_/g, " ")}
          </h3>
          <p className="ui-sub" style={{ marginBottom: 12 }}>{gap.reason}</p>

          <div className="ui-grid tight">
            {gap.products.map((p) => (
              <a
                key={p.id}
                className="ui-card"
                href={p.url}
                target="_blank"
                rel="noopener noreferrer sponsored"
                /* rel="sponsored" is required markup for a paid link, not a
                   nicety — search engines and regulators both read it. */
                onClick={() => {
                  void recordShopClick(p.id);
                  setVisited((v) => ({ ...v, [p.id]: true }));
                }}
                style={{ textDecoration: "none", display: "block" }}
              >
                <div className="ui-frame photo" style={{ aspectRatio: "1 / 1" }}>
                  {p.image_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={p.image_url} alt="" loading="lazy" />
                  ) : (
                    <span className="ui-ph">{p.subcategory?.replace(/_/g, " ") ?? p.slot}</span>
                  )}
                </div>
                <div className="ui-cbody">
                  <span className="ui-name" style={{ fontSize: 13.5 }}>{p.title}</span>
                  <p className="ui-sub">
                    {price(p.price_minor, p.currency)}
                    {p.brand ? ` · ${p.brand}` : ""}
                  </p>
                </div>
              </a>
            ))}
          </div>

          {/* The buy-confirmation row sits OUTSIDE the product anchors: a
              button inside an <a> is invalid markup and, worse, a tap meant
              for "I bought this" would navigate to the merchant instead. */}
          {gap.products.some((p) => visited[p.id] || owned[p.id]) ? (
            <div style={{ marginTop: 10 }}>
              {gap.products
                .filter((p) => visited[p.id] || owned[p.id])
                .map((p) => (
                  <div key={p.id} style={{ marginBottom: 6 }}>
                    {owned[p.id] ? (
                      <p className="ui-sub" style={{ fontSize: 12 }}>
                        ✓ {p.title} — {owned[p.id]}
                      </p>
                    ) : (
                      <>
                        {data.auto_add ? (
                          <p className="ui-sub" style={{ fontSize: 12, marginBottom: 4 }}>
                            Bought {p.title} through this link? It will appear in your
                            wardrobe on its own once the shop confirms the order.
                          </p>
                        ) : null}
                        <button
                          className="ui-btn"
                          disabled={adding === p.id}
                          onClick={() => {
                            setAdding(p.id);
                            ownProduct(p.id)
                              .then((r) => setOwned((o) => ({ ...o, [p.id]: r.note })))
                              .catch((e) =>
                                setOwned((o) => ({
                                  ...o,
                                  [p.id]: e instanceof Error ? e.message : String(e),
                                })),
                              )
                              .finally(() => setAdding(null));
                          }}
                        >
                          {adding === p.id
                            ? "Adding…"
                            : data.auto_add
                              ? "Don't want to wait? Add it now"
                              : `I bought this — add “${p.title}”`}
                        </button>
                      </>
                    )}
                  </div>
                ))}
            </div>
          ) : null}
        </div>
      ))}

      {/* Verbatim from the server. */}
      <p className="ui-sub" style={{ fontSize: 11.5 }}>{data.affiliate}</p>
    </section>
  );
}
