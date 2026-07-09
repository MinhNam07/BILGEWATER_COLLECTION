# Scraper API (backend contract)

Read-only endpoints for the Bilgewater daily collector. Authenticated via a static Bearer token (`SCRAPER_API_SECRET` on the server, `SCRAPER_API_TOKEN` in GitHub Actions).

## Endpoints

### List with prices

```
GET /api/scraper/cards-with-prices
Authorization: Bearer <SCRAPER_API_SECRET>
```

Response: same JSON shape as public `GET /api/cards-with-prices` (e.g. `{ "cards": [ ... ] }`).

### Card detail

```
GET /api/scraper/cards/detail?card_id=UNL-015&print_variation=normal
Authorization: Bearer <SCRAPER_API_SECRET>
```

Response: same JSON shape as public `GET /api/cards/detail` for the given card.

## Implementation notes (bilgewater backend)

1. Reuse existing handlers that power the public API; add a route group `/api/scraper/*` with middleware that validates `Authorization: Bearer` against `SCRAPER_API_SECRET` env.
2. Do **not** require Firebase App Check on these routes.
3. Rate limit separately (e.g. 60 requests/minute per token).
4. GET only — no mutations.
5. Log `User-Agent` and request path for audit.

## Collector configuration

```bash
SCRAPER_API_TOKEN=<same value as SCRAPER_API_SECRET>
SCRAPER_API_BASE=https://api.bilgewatermarket.com  # optional
```

When `SCRAPER_API_TOKEN` is set, the collector tries the scraper API before Playwright.
