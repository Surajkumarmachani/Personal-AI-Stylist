# catalogue/

CSV feeds for the shopping gap-filler. `sample.csv` exists so the feature is
demoable and testable **without an affiliate account** — swapping in a real
merchant is a provider change (`packages/stylist_shop/providers.py`), not a
redesign.

## Columns

`external_id,title,url,slot,brand,subcategory,primary_colour,dress_code,formality,warmth,price_minor,currency,image_url,in_stock,gender`

`gender` is the shop department — `women`, `men` or `unisex` (blank = unisex).
Shop suggestions only offer a user their own line plus unisex.

`slot`, `subcategory`, `primary_colour` and `dress_code` **must be taxonomy
values** — a product that cannot be described in the wardrobe's vocabulary can
never be matched to a wardrobe gap, so the loader rejects it rather than
storing a row no query will return.

`price_minor` is in minor units (paise, cents), matching
`garments.purchase_price_minor`.

## The links in sample.csv are search URLs, not product pages

Deliberately. A hardcoded product URL rots — the item sells out, the page 404s,
and a dead link is worse than no link. These point at a retailer's search so
they stay valid; a real feed supplies real product URLs and real stock, which
is exactly what `in_stock` and the nightly refresh are for.

## Purchases join the wardrobe on their own (affiliate postback)

When `SHOP_POSTBACK_SECRET` is set, every link `/shop/gaps` returns carries a
reference in the `SHOP_SUBID_PARAM` query parameter (default `subid`). Register
this postback URL with the affiliate network, using its own macro names:

    https://<api-host>/shop/conversions?secret=<SHOP_POSTBACK_SECRET>&network=<name>&ref={subid}&conversion_id={order_id}&status={status}

- `pending` / `approved` adds the product to that user's wardrobe (once — a
  resent report updates the same row).
- `rejected` / `cancelled` / `returned` hides it again, unless the user has
  since worn it, corrected its tags, or added it themselves.
- An unknown status gets a 400, so the network shows the error to whoever set
  it up; nothing is guessed.

Unset, links go out untagged, the endpoint refuses every call, and "I bought
this" is the only way in.
