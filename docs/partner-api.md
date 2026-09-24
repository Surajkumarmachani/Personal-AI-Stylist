# AI Stylist — Partner API

Call the stylist from your own servers: create a user for each of your
customers, add their clothes, and ask for outfits, all with an API key.

Interactive reference for every endpoint: `https://<api-host>/docs`
(click **Authorize** and paste your key).

## 1. Authentication

Every request carries your key:

```
X-API-Key: sty_XXXXXXXXXXXX_…
```

Calls made **for one of your users** also carry your own ID for that user:

```
X-User-Id: cust_123
```

Keep the key on your server. It gives access to every user you create, so
never put it in a browser or mobile app. If it leaks, ask us to revoke it and
issue a new one; revocation takes effect on the next request.

## 2. Create a user

```bash
curl -X POST https://<api-host>/partner/users \
  -H "X-API-Key: $STYLIST_KEY" -H "Content-Type: application/json" \
  -d '{"external_id": "cust_123", "dresses_as": "women"}'
```

- `external_id` — your ID for the person (up to 128 characters). Unique
  within your account.
- `dresses_as` — optional: `women`, `men` or `all`. Controls which shop
  products and styling advice they see. Their own wardrobe is never filtered.

Returns `201` when created and `200` if it already existed, so it's safe to
call before every session.

## 3. Ask the stylist

```bash
curl -X POST https://<api-host>/chat \
  -H "X-API-Key: $STYLIST_KEY" -H "X-User-Id: cust_123" \
  -H "Content-Type: application/json" \
  -d '{"message": "what should I wear to a friend'\''s haldi?"}'
```

The response has `reply` (a sentence to show), `outfits` (built only from
clothes the user owns), and `notes`. When the wardrobe can't dress the
occasion, `outfits` is empty and `GET /shop/gaps?occasion=<id>` returns
products that would fill the gap.

## 4. Common endpoints

All of these take `X-API-Key` + `X-User-Id`.

| What | Endpoint |
|---|---|
| Ask in plain language | `POST /chat` |
| Outfits for an occasion | `GET /suggestions?occasion=wedding_reception` |
| Upload clothes (get an upload URL, then ingest) | `POST /uploads/presign`, `POST /garments/ingest` |
| List the wardrobe | `GET /garments` |
| They wore something | `POST /garments/{id}/wear` |
| In / out of the wash | `PATCH /garments/{id}/laundry` `{"needs_wash": true}` |
| Products to fill a gap | `GET /shop/gaps?occasion=…` |
| Clothing preference | `GET` / `PUT /me/dresses-as` |
| Their city (for weather) | `PUT /me/location` `{"place": "Mumbai"}` |
| Export their data | `POST /me/export` |
| Delete the user and all their data | `DELETE /me?confirm=DELETE` (then `GET /me/erasure` for progress) |

Anything in the wash, or worn in the last 3 days, is left out
of suggestions automatically.

## 5. Errors

| Status | Meaning |
|---|---|
| `400` | `X-User-Id` missing, or a bad request body |
| `401` | Key missing, wrong, or revoked |
| `404` | No user with that `X-User-Id` — create it with `POST /partner/users` |
| `429` | Rate limit reached; wait `Retry-After` seconds |

The rate limit is per partner account, across all of your keys (default 120
requests per minute).

## For the admin of this deployment

Admins (users with `is_admin`) manage partners with a normal login token:

| What | Endpoint |
|---|---|
| Create a partner | `POST /admin/api-clients` `{"name": "Acme", "rate_limit_per_minute": 120}` |
| Issue a key (shown once) | `POST /admin/api-clients/{client_id}/keys` `{"label": "production"}` |
| List partners and keys | `GET /admin/api-clients` |
| Revoke a key | `DELETE /admin/api-keys/{key_id}` |

Keys are stored only as a hash. A key that's lost can't be recovered;
revoke it and issue a new one.
