# web — Next.js client

**Not built yet.** Reserved by the Step 1.1 monorepo layout; the first real
screen is the Phase 2.4 wardrobe grid (cutouts from signed URLs, with per-item
state badges for `processing` / `ready` / `needs review`).

Phase 1 deliberately ships no UI. The API contract it will consume is already
live and browsable at <http://localhost:8080/docs> — the generated OpenAPI spec
is the contract clients get tested against (§D2), so build against that rather
than reading the router source.

One client-side note that will matter here: uploads use presigned **POST**, not
PUT — a multipart form to `url` carrying every entry of `fields` plus a `file`
part. See `packages/stylist_clients/storage.py` for why (a PUT URL cannot cap
the body size, so the ceiling would not be enforced anywhere).
