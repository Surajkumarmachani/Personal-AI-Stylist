"""Merchant-reported purchases: the half of "did they buy it" we CAN observe.

`own.py` says plainly that we never see the checkout. An affiliate network
does, and reports each order to a postback URL we register with it, echoing
back a reference we attached to the outbound link. This module is the pure
part of that loop — putting the reference on the link and reading the status
off the report — so the router only does IO.

NETWORK-AGNOSTIC, LIKE THE PROVIDERS
------------------------------------
Cuelinks, vCommission, Admitad, EarnKaro and Impact all support a
server-to-server postback with a sub-id macro, and all name things
differently: `subid`, `aff_sub`, `sub1`; `approved`, `confirmed`, `1`. The
parameter name is configuration, and every status spelling seen is folded
into three states here. An unrecognised status is refused rather than
guessed, because guessing "approved" puts a garment in someone's wardrobe.

WHAT A REPORT DOES NOT PROVE
----------------------------
A conversion says an order happened after the click, not which item was in
it — someone who clicked a kurta may have bought socks. The garment it creates
is therefore flagged `needs_review` like every catalogue garment, and records
`confirmed_by="conversion_feed"` so it can always be told apart from one the
user stated.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Every spelling observed across the networks above, folded to three states.
# `pending` still adds the garment: networks hold orders for 30-60 days before
# approving, and "it appears in two months" is not what automatic means.
_STATUS = {
    "pending": "pending",
    "0": "pending",
    "open": "pending",
    "placed": "pending",
    "approved": "approved",
    "confirmed": "approved",
    "1": "approved",
    "paid": "approved",
    "completed": "approved",
    "rejected": "rejected",
    "declined": "rejected",
    "cancelled": "rejected",
    "canceled": "rejected",
    "returned": "rejected",
    "refunded": "rejected",
    "2": "rejected",
}


def normalise_status(raw: str | None) -> str | None:
    """'pending', 'approved', 'rejected', or None for a spelling we do not know."""
    return _STATUS.get(str(raw or "").strip().lower())


def tracked_url(url: str, ref: str, param: str) -> str:
    """The merchant link with our reference attached as `param`.

    An existing value for `param` is REPLACED, not duplicated: a catalogue
    link that already carries a placeholder sub-id would otherwise send two,
    and networks disagree about which one wins.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, ref))
    return urlunsplit(parts._replace(query=urlencode(query)))
