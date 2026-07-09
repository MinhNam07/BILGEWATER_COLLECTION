# Noscript / SSR verification (2026-07-09)

Verified with:

```bash
curl -sS "https://bilgewatermarket.com/cards/UNL-015" | rg -A15 'noscript><section'
```

## Finding

Initial HTML **does** include card metadata in `<noscript class="seo-prerender">`:

- Title: `Right of Conquest Price`
- Card number: `UNL-015/219`
- Rarity: `Uncommon`
- Chinese name: `占山为王`
- Variant: `Normal`

Prices are **not** present in noscript (only "Price history: Available when market observations exist").

Playwright `page.content()` after React hydration may show a generic noscript shell on failed detail loads (see `data/targeted/debug/*`) because the client-rendered DOM differs from raw SSR.

## Recommendation

Per plan Phase 3: defer noscript parser until scraper API (`SCRAPER_API_TOKEN`) is deployed. Noscript can supplement catalog metadata later but cannot replace price collection.
