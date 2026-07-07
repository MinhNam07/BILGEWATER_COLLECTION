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
| `--verbose` | off | Debug logging |

Examples:

```bash
python scrape.py --limit 20
python scrape.py --no-headless --verbose
python scrape.py --output-dir /tmp/bilgewater --db-path /tmp/bilgewater.sqlite
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
| `price` | string | `0.50` | Normalized decimal string; CN price preferred, EN fallback |
| `url` | string | `https://bilgewatermarket.com/cards/UNL-001?print_variation=foiled` | Card detail link |
| `source_url` | string | `https://bilgewatermarket.com/cards` | Page scraped |
| `collected_at` | ISO 8601 UTC | `2026-07-07T12:34:56+00:00` | Collection timestamp |
| `raw_text` | string | `Arena Kingpin UNL-001/219 Foiled CN ¥0.50 EN $0.12` | Original tile text (truncated) |

Foil and non-foil variants are separate rows (different URLs). Showcase printings are recorded as `foil_status=unknown`. Currency symbols are stripped from `price`; see `raw_text` for CN/EN context.

## Daily cron (local server)

Vietnam midnight:

```bash
0 0 * * * cd /path/to/bilgewater_collector && .venv/bin/python scrape.py >> data/collector.log 2>&1
```

## GitHub Actions

Workflow: [`.github/workflows/daily.yml`](.github/workflows/daily.yml)

- **Schedule:** `0 17 * * *` UTC = 00:00 Asia/Ho_Chi_Minh
- **Manual run:** Actions tab → "Daily Bilgewater Collect" → "Run workflow"
- **Artifact:** `bilgewater-data-<run_id>` uploaded for 30 days (CSV, JSONL, SQLite)
- **Auto-commit:** Scraped `data/` is committed and pushed to the repo

No secrets are required. `USER_AGENT` uses the repository owner as contact.

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

- **JavaScript SPA:** Requires Playwright; static HTTP fetch will not work.
- **Public pages only:** Does not bypass login, CAPTCHA, paywalls, or anti-bot controls.
- **Infinite scroll:** Loads cards in batches (~50 per scroll); full catalog takes ~1–2 minutes.
- **Price field:** One decimal price per row (CN preferred). Both CN and EN prices remain in `raw_text`.
- **Selector drift:** If Bilgewater changes markup, DOM selectors may need updates; regex fallback is included.
- **GitHub cron delay:** Scheduled workflows may start minutes late; not guaranteed at exact midnight.
- **Rate limiting:** Run low-frequency only; polite delays are built in.
# BILGEWATER_COLLECTION
