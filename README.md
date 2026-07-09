# Bilgewater Daily Collector

Collects daily visible card market data from [Bilgewater Market](https://bilgewatermarket.com/cards).

## Install

Requires Python 3.11+ (3.13 recommended; Python 3.14 is not yet supported by Playwright).

```bash
python3.13 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Edit `.env` and set a contact email in `USER_AGENT` (e.g. `BilgewaterDailyCollector/1.0 contact=you@example.com`).

## Run

Default (headless, writes to `data/`):

```bash
python scrape.py
```

### CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--urls` | `START_URLS` env or `/cards` | Comma-separated pages to scrape |
| `--output-dir` | `data` | Output directory for CSV/JSONL |
| `--db-path` | `data/bilgewater.sqlite` | SQLite database path |
| `--headless` / `--no-headless` | headless on | Browser visibility |
| `--limit N` | none | Cap cards collected (testing) |
| `--concurrency` | `10` | Concurrent detail pages (seed/enrich/targeted) |
| `--detail-timeout-ms` | `12000` | Per-detail navigation timeout |
| `--total-timeout-sec` | `2700` full / `0` targeted | Wall-clock deadline; timeout fails without saving |
| `--max-detail-retries` | `1` | Extra attempt per detail URL |
| `--max-debug-artifacts` | `15` | Cap PNG/HTML failure samples |
| `--fail-rate-abort` | `0.30` | Abort seeded full scrape above this fail rate |
| `--fail-rate-min-samples` | `40` | Min decisions before fail-rate abort |
| `--verbose` | off | Debug logging |

Full refresh prefers the `/cards` list/grid. Detail pages run concurrently only for missing fields or when the list returns 0 (seed from `web/cards.json`). Target runtime is **20–45 minutes**; runs that exceed `--total-timeout-sec` exit without overwriting data.

Examples:

```bash
python scrape.py --limit 30 --verbose
python scrape.py --no-headless --verbose
python scrape.py --output-dir /tmp/bilgewater --db-path /tmp/bilgewater.sqlite
python scrape.py --urls 'https://bilgewatermarket.com/cards/UNL-001'
```
## Output files

Each run writes:

- `data/bilgewater_YYYY-MM-DD.csv`
- `data/bilgewater_latest.csv` (same as today's CSV; stable URL for Google Sheets)
- `data/bilgewater_YYYY-MM-DD.jsonl`
- `data/bilgewater.sqlite` (append-only history)

## Output schema

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `name` | string | `Arena Kingpin` | Card name from page heading |
| `foil_status` | string | `foil`, `nonfoil`, `unknown` | Derived from URL/query and page text |
| `price` | string | `0.12` | Normalized USD decimal string; EN price preferred, CN fallback |
| `url` | string | `https://bilgewatermarket.com/cards/UNL-001?print_variation=foiled` | Card detail link |
| `source_url` | string | `https://bilgewatermarket.com/cards` | Page scraped |
| `collected_at` | ISO 8601 UTC | `2026-07-07T12:34:56+00:00` | Collection timestamp |
| `raw_text` | string | `Arena Kingpin UNL-001/219 Foiled CN ¥0.50 EN $0.12` | Original tile text (truncated) |

Foil and non-foil variants are separate rows (different URLs). Showcase printings are recorded as `foil_status=unknown`. `price` is the EN (USD) market price; see `raw_text` for both CN and EN values.

## Daily cron (local server)

Vietnam midnight:

```bash
0 0 * * * cd /path/to/bilgewater_collector && .venv/bin/python scrape.py >> data/collector.log 2>&1
```

## GitHub Actions

Workflow: [`.github/workflows/daily.yml`](.github/workflows/daily.yml)

- **Schedule:** `0 17 * * *` UTC = 00:00 Asia/Ho_Chi_Minh
- **Manual run:** Actions tab → "Daily Bilgewater Collect" → "Run workflow"
- **Job timeout:** 50 minutes (scrape budget 45 minutes for full refresh)
- **Artifact:** `bilgewater-data-<run_id>` uploaded for 30 days (CSV, JSONL, SQLite)
- **Auto-commit:** Scraped `data/` is committed and pushed to the repo (skipped if scrape fails)

No secrets are required for Playwright-only mode. `USER_AGENT` uses the repository owner as contact.

### Scraper API (recommended for CI)

When Firebase App Check blocks headless browsers, set `SCRAPER_API_TOKEN` (GitHub secret `SCRAPER_API_TOKEN`) to use the read-only backend endpoints documented in [`docs/scraper-api-backend.md`](docs/scraper-api-backend.md). The collector tries this API before Playwright.

```bash
SCRAPER_API_TOKEN=your-secret python scrape.py
```

Each run writes `data/.scrape_summary.json` with `skipped_api_auth`, `api_auth_only_abort`, and row counts. If every failure is App Check (401), existing CSV/JSONL files are **not** overwritten and the process exits 0 with a warning.

## Google Sheets (auto-update daily)

Uses [Google Apps Script](scripts/google_apps_script.js) to pull `bilgewater_latest.csv` from your public GitHub repo.

### 1. Push repo to GitHub

Repo must be **public** (or use a personal access token in the script — see below). After the daily workflow runs, `data/bilgewater_latest.csv` is committed automatically.

Raw URL format:

```
https://raw.githubusercontent.com/<user>/<repo>/main/data/bilgewater_latest.csv
```

### 2. Create the Sheet

1. Open [Google Sheets](https://sheets.google.com) → create a new spreadsheet.
2. **Extensions → Apps Script**.
3. Paste the contents of [`scripts/google_apps_script.js`](scripts/google_apps_script.js).
4. Edit `CSV_URL` at the top with your GitHub raw URL.
5. **Save** → **Run** `refreshBilgewater` once (grant permissions when prompted).
6. **Triggers** (clock icon) → **Add trigger**:
   - Function: `refreshBilgewater`
   - Event: **Time-driven** → **Day timer** → **1am to 2am** (after GitHub Actions at 00:00 VN)

The script creates two tabs:

- **Data** — latest card prices (refreshed daily)
- **Log** — refresh timestamp and row count

### Private repo

Replace the fetch call with a token header:

```javascript
const TOKEN = "ghp_..."; // GitHub PAT with repo read access
const response = UrlFetchApp.fetch(CSV_URL, {
  headers: { Authorization: "Bearer " + TOKEN },
  muteHttpExceptions: true,
});
```

Store the token in **Apps Script → Project settings → Script properties** (`GITHUB_TOKEN`) instead of hardcoding it.

## Known limitations

- **JavaScript SPA:** Default path uses Playwright; optional scraper API avoids DOM when `SCRAPER_API_TOKEN` is set.
- **App Check false negatives:** React may show "Card not found" when the API returns 401; the collector classifies this as `skipped_api_auth` and preserves existing data.
- **Public pages only:** Does not bypass login, CAPTCHA, paywalls, or anti-bot controls (except the official scraper API token).
- **Infinite scroll:** Loads cards in batches (~50 per scroll); full catalog takes ~1–2 minutes.
- **Price field:** One decimal USD price per row (EN preferred). Both CN and EN prices remain in `raw_text`.
- **Selector drift:** If Bilgewater changes markup, DOM selectors may need updates; regex fallback is included.
- **GitHub cron delay:** Scheduled workflows may start minutes late; not guaranteed at exact midnight.
- **Rate limiting:** Run low-frequency only; polite delays are built in.

## Riftbound Card Tracker (Web)

Mobile-friendly inventory tracker at [`web/`](web/) — merges Bilgewater prices with Riftbound card metadata (type, images) and syncs your collection across devices via Supabase.

### Local preview

```bash
python scripts/build_cards_json.py
cp web/config.example.js web/config.js   # add Supabase keys for sync
python3 -m http.server 8080 --directory web
```

Open http://localhost:8080

### Build cards.json

Merges `data/bilgewater_latest.csv` with the [RiftScribe API](https://riftscribe.gg/api-docs):

```bash
python scripts/build_cards_json.py
```

Output: `web/cards.json` (~1,500+ cards with prices, types, and images).

### Supabase setup (cross-device sync)

1. Create a free project at [supabase.com](https://supabase.com).
2. **SQL Editor** → run [`scripts/supabase/schema.sql`](scripts/supabase/schema.sql).
3. **Database → Replication** → enable the `inventory` table for Realtime.
4. Copy **Project URL** and **anon public key** from **Settings → API**.

### Deploy to Vercel

1. Push repo to GitHub.
2. Import on [vercel.com](https://vercel.com) → select this repo.
3. Vercel reads [`vercel.json`](vercel.json) automatically:
   - Build: regenerates `cards.json` + `config.js`
   - Output: `web/` directory
4. Add environment variables in Vercel project settings:
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
5. Deploy. Each push (including daily GitHub Actions data updates) triggers a rebuild with fresh prices.

### Using sync across devices

1. Open the deployed site on your phone or computer.
2. Tap the gear icon → copy your **sync code**.
3. On another device, paste the code in Settings → **Apply**.
4. Inventory changes sync via Supabase (Realtime when enabled).

Without Supabase configured, the app works in **local-only** mode (inventory stored per browser).
