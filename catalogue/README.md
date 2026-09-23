# catalogue/

CSV feeds for the shopping gap-filler. `sample.csv` exists so the feature is
demoable and testable **without an affiliate account** — swapping in a real
merchant is a provider change (`packages/stylist_shop/providers.py`), not a
redesign.

## Columns

`external_id,title,url,slot,brand,subcategory,primary_colour,dress_code,formality,warmth,price_minor,currency,image_url,in_stock`

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
