import argparse
import asyncio
import csv
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

log = logging.getLogger("bilgewater")

PRICE_RE = re.compile(
    r"(?i)(?:CN¥|US\$|SGD|USD|CNY|RMB|[$¥￥€£])\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:RMB|CNY|USD|SGD)"
)
FOIL_RE = re.compile(
    r"(?i)\b(cold foil|rainbow foil|foil(?:ed)?|holo|non[-\s]?foil|normal|standard)\b"
)
BAD_NAME_RE = re.compile(
    r"(?i)^(price|foil|foiled|market|ranking|rarity|set|variant|cn|en|change|search)$"
)
CARD_SELECTOR = 'a.block[href*="/cards/"]'
SCROLL_DELAY_MS = 800
SCROLL_STEP_PX = 2500
STABLE_ROUNDS = 3
MAX_SCROLL_ROUNDS = 100
PAGE_TIMEOUT_MS = 60_000
NETWORK_SETTLE_MS = 2_000

EXTRACT_CARDS_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };

  const cards = [];
  for (const anchor of document.querySelectorAll('a.block[href*="/cards/"]')) {
    if (!visible(anchor)) continue;

    const h3 = anchor.querySelector('h3');
    const name = h3 ? h3.innerText.trim() : '';
    const href = anchor.href;
    const text = (anchor.innerText || '').trim();

    let cnPrice = null;
    let enPrice = null;
    for (const row of anchor.querySelectorAll('.flex.items-center.justify-between')) {
      const label = row.querySelector('span.text-muted-foreground, span.text-xs');
      const value = row.querySelector('.font-mono');
      if (!label || !value) continue;
      const market = label.innerText.trim().toUpperCase();
      const priceText = value.innerText.trim();
      if (market === 'CN') cnPrice = priceText;
      if (market === 'EN') enPrice = priceText;
    }

    const isFoil = href.includes('print_variation=foiled') || /\bfoiled\b/i.test(text);
    const isShowcase = href.includes('print_variation=showcase') || /\bshowcase\b/i.test(text);
    let printVariation = 'normal';
    try {
      const u = new URL(href);
      const pv = (u.searchParams.get('print_variation') || '').toLowerCase();
      if (pv) printVariation = pv;
    } catch {}

    const imgEl = anchor.querySelector('img');
    const imageUrl = imgEl ? (imgEl.currentSrc || imgEl.src || '') : '';

    cards.push({
      name,
      href,
      text,
      cnPrice,
      enPrice,
      isFoil,
      isShowcase,
      printVariation,
      imageUrl,
    });
  }
  return cards;
}
"""

EXTRACT_DETAIL_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  const titleEl = document.querySelector('h1') || document.querySelector('[data-testid="card-name"]');
  const name = norm(titleEl ? titleEl.textContent : '');

  // Try to find CN/EN price blocks (same pattern as tiles), then fall back to text scan.
  let cnPrice = null;
  let enPrice = null;
  for (const row of document.querySelectorAll('.flex.items-center.justify-between')) {
    const label = row.querySelector('span.text-muted-foreground, span.text-xs');
    const value = row.querySelector('.font-mono');
    if (!label || !value) continue;
    const market = norm(label.textContent).toUpperCase();
    const priceText = norm(value.textContent);
    if (market === 'CN') cnPrice = priceText;
    if (market === 'EN') enPrice = priceText;
  }

  // Pick the most likely "main card art" image: largest visible image.
  let best = { area: 0, url: '' };
  for (const img of document.querySelectorAll('img')) {
    if (!visible(img)) continue;
    const r = img.getBoundingClientRect();
    const area = r.width * r.height;
    const url = img.currentSrc || img.src || '';
    if (!url) continue;
    if (area > best.area) best = { area, url };
  }

  const rawText = norm(document.body ? document.body.innerText : '');
  return {
    name,
    cnPrice,
    enPrice,
    imageUrl: best.url || '',
    text: rawText.slice(0, 3000),
  };
}
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize_price(text: str | None) -> str | None:
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        digits = re.sub(r"[^\d.]", "", text)
        if not digits:
            return None
        try:
            return f"{float(digits):.2f}"
        except ValueError:
            return None
    raw = (m.group(1) or m.group(2) or "").replace(",", "")
    try:
        return f"{float(raw):.2f}"
    except ValueError:
        return None


def normalize_foil_from_dom(is_foil: bool, text: str = "", is_showcase: bool = False) -> str:
    # Showcase is an alternate print, not inherently foil.
    if is_showcase:
        return "nonfoil"
    if is_foil:
        return "foil"
    low = (text or "").lower()
    if re.search(r"non[-\s]?foil|normal|standard", low):
        return "nonfoil"
    if re.search(r"cold foil|rainbow foil|\bfoiled\b|holo", low):
        return "foil"
    return "nonfoil"


def parse_foil(text: str) -> str:
    low = text or ""
    if re.search(r"non[-\s]?foil|normal|standard", low, re.I):
        return "nonfoil"
    if re.search(r"cold foil|rainbow foil|foil(?:ed)?|holo", low, re.I):
        return "foil"
    return "unknown"


def parse_name(text: str, href: str | None) -> str:
    lines = [re.sub(r"\s+", " ", x).strip() for x in (text or "").splitlines()]
    for line in lines:
        if not line or len(line) < 2 or PRICE_RE.search(line) or BAD_NAME_RE.match(line):
            continue
        if len(line) <= 80:
            return line
    if href:
        slug = href.rstrip("/").split("/")[-1].split("?")[0]
        return re.sub(r"[-_]+", " ", slug).strip().title()
    return "unknown"


def extract_market_price(text: str, market: str) -> str | None:
    m = re.search(
        rf"(?i)\b{re.escape(market)}\s*(?:US\$|[$¥￥])\s*(\d[\d,]*(?:\.\d+)?)",
        text or "",
    )
    if not m:
        return None
    try:
        return f"{float(m.group(1).replace(',', '')):.2f}"
    except ValueError:
        return None


def parse_price(text: str) -> str | None:
    return extract_market_price(text, "EN") or extract_market_price(text, "CN") or normalize_price(text)

SET_NUMBER_URL_RE = re.compile(r"/cards/([A-Z]+)-(\d+)([A-Z])?", re.I)


def is_cards_index_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    path = (p.path or "").rstrip("/")
    return path.lower().endswith("/cards")


def is_detail_card_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    path = (p.path or "")
    return bool(re.search(r"/cards/[A-Za-z]+-\d+[A-Za-z]?(?:/)?$", path))


def parse_print_variation_from_url(url: str) -> str:
    try:
        q = parse_qs(urlparse(url).query or "")
        pv = (q.get("print_variation") or [""])[0].strip().lower()
        return pv or "normal"
    except Exception:
        return "normal"


def parse_set_number_from_url(url: str) -> str | None:
    m = SET_NUMBER_URL_RE.search(url or "")
    if not m:
        return None
    set_code = m.group(1).upper()
    num = int(m.group(2))
    suffix = (m.group(3) or "").upper()
    return f"{set_code}-{num:03d}{suffix}"


def infer_foil_from_print_variation(print_variation: str, fallback: str) -> str:
    pv = (print_variation or "").lower()
    if pv == "foiled":
        return "foil"
    if pv:
        return "nonfoil"
    return fallback

def fingerprint(row: dict) -> str:
    key = "|".join(
        [
            row.get("source_url", ""),
            row.get("url", ""),
            row.get("name", ""),
            row.get("foil_status", ""),
            row.get("print_variation", ""),
            row.get("price", ""),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen_fp: set[str] = set()
    seen_url: set[str] = set()
    out: list[dict] = []
    for row in rows:
        fp = fingerprint(row)
        url_key = row.get("url", "").strip().lower()
        if fp in seen_fp:
            continue
        if url_key and url_key in seen_url:
            continue
        seen_fp.add(fp)
        if url_key:
            seen_url.add(url_key)
        out.append(row)
    return out


def build_row(
    *,
    source_url: str,
    base_url: str,
    name: str,
    href: str | None,
    text: str,
    cn_price: str | None = None,
    en_price: str | None = None,
    is_foil: bool | None = None,
    is_showcase: bool = False,
    print_variation: str | None = None,
    image_url: str | None = None,
    set_number: str | None = None,
) -> dict | None:
    abs_url = urljoin(base_url, href) if href else source_url
    price = normalize_price(en_price) or normalize_price(cn_price) or parse_price(text)
    if not price:
        return None

    resolved_name = name.strip() if name else parse_name(text, abs_url)
    if is_foil is None:
        foil_status = parse_foil(text)
    else:
        foil_status = normalize_foil_from_dom(is_foil, text, is_showcase)

    if foil_status == "unknown" and "print_variation=foiled" in abs_url:
        foil_status = "foil"
    elif foil_status == "unknown" and "print_variation=showcase" in abs_url:
        foil_status = "nonfoil"
    elif foil_status == "unknown" and "/cards/" in abs_url:
        foil_status = "nonfoil"

    pv = (print_variation or "").strip().lower()
    if not pv:
        if "print_variation=showcase" in abs_url:
            pv = "showcase"
        elif "print_variation=foiled" in abs_url:
            pv = "foiled"
        else:
            pv = "normal"

    foil_status = infer_foil_from_print_variation(pv, foil_status)

    return {
        "collected_at": now_iso(),
        "source_url": source_url,
        "name": resolved_name,
        "foil_status": foil_status,
        "price": price,
        "url": abs_url,
        "print_variation": pv,
        "image_url": (image_url or "").strip(),
        "cn_price": (cn_price or "").strip() if cn_price else "",
        "en_price": (en_price or "").strip() if en_price else "",
        "set_number": (set_number or "").strip() if set_number else "",
        "raw_text": re.sub(r"\s+", " ", text).strip()[:1000],
    }


async def count_cards(page) -> int:
    return await page.evaluate(
        f"() => document.querySelectorAll({json.dumps(CARD_SELECTOR)}).length"
    )


async def click_load_more(page) -> bool:
    clicked = await page.evaluate(
        """() => {
      const candidates = [...document.querySelectorAll('button, a')].filter(el => {
        const t = (el.innerText || '').trim().toLowerCase();
        return /load more|show more|view more/.test(t);
      });
      const btn = candidates.find(el => !el.disabled);
      if (!btn) return false;
      btn.click();
      return true;
    }"""
    )
    if clicked:
        log.info("clicked load-more button")
        await page.wait_for_timeout(SCROLL_DELAY_MS)
    return clicked


async def load_all_cards(page, limit: int | None = None) -> None:
    prev_count = 0
    stable_rounds = 0
    scroll_round = 0

    while scroll_round < MAX_SCROLL_ROUNDS:
        count = await count_cards(page)
        log.info("scroll round %d: %d card tiles visible", scroll_round + 1, count)

        if limit and count >= limit:
            log.info("reached limit %d (visible %d)", limit, count)
            break

        if count == prev_count:
            stable_rounds += 1
            if stable_rounds >= STABLE_ROUNDS:
                if await click_load_more(page):
                    stable_rounds = 0
                    prev_count = await count_cards(page)
                    continue
                log.info("card count stable at %d", count)
                break
        else:
            stable_rounds = 0

        prev_count = count
        scroll_round += 1
        await page.mouse.wheel(0, SCROLL_STEP_PX)
        await page.wait_for_timeout(SCROLL_DELAY_MS)


async def extract_candidates(
    page, source_url: str, base_url: str, limit: int | None = None
) -> list[dict]:
    raw = await page.evaluate(EXTRACT_CARDS_JS)
    rows: list[dict] = []

    for item in raw:
        row = build_row(
            source_url=source_url,
            base_url=base_url,
            name=item.get("name", ""),
            href=item.get("href"),
            text=item.get("text", ""),
            cn_price=item.get("cnPrice"),
            en_price=item.get("enPrice"),
            is_foil=item.get("isFoil"),
            is_showcase=item.get("isShowcase", False),
            print_variation=item.get("printVariation"),
            image_url=item.get("imageUrl"),
        )
        if row:
            rows.append(row)
        if limit and len(rows) >= limit:
            break

    if not rows:
        log.warning("DOM extraction empty for %s, using regex fallback", source_url)
        rows = await extract_candidates_fallback(page, source_url, base_url, limit)

    deduped = dedupe_rows(rows)
    log.info("extracted %d rows (%d before dedupe) from %s", len(deduped), len(rows), source_url)
    return deduped[:limit] if limit else deduped


async def extract_detail_row(page, url: str, base_url: str) -> dict | None:
    item = await page.evaluate(EXTRACT_DETAIL_JS)
    name = (item.get("name") or "").strip()
    text = (item.get("text") or "").strip()
    cn_price = item.get("cnPrice")
    en_price = item.get("enPrice")
    image_url = item.get("imageUrl")
    pv = parse_print_variation_from_url(url)
    set_number = parse_set_number_from_url(url) or ""

    return build_row(
        source_url=url,
        base_url=base_url,
        name=name,
        href=url,
        text=text or name,
        cn_price=cn_price,
        en_price=en_price,
        is_foil=None,
        is_showcase=(pv == "showcase"),
        print_variation=pv,
        image_url=image_url,
        set_number=set_number,
    )


async def extract_candidates_fallback(
    page, source_url: str, base_url: str, limit: int | None = None
) -> list[dict]:
    js = r"""
    () => {
      const priceRe = /(?:CN¥|US\$|SGD|USD|CNY|RMB|[$¥￥€£])\s*\d[\d,.]*(?:\.\d+)?|\d[\d,.]*(?:\.\d+)?\s*(?:RMB|CNY|USD|SGD)/i;
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const out = [];
      for (const el of document.querySelectorAll('a, article, li, tr, div')) {
        if (!visible(el)) continue;
        const text = (el.innerText || '').trim();
        if (!text || text.length > 1500 || !priceRe.test(text)) continue;
        const linkEl = el.closest('a') || el.querySelector('a');
        out.push({ text, href: linkEl ? linkEl.href : null });
      }
      return out;
    }
    """
    raw = await page.evaluate(js)
    rows: list[dict] = []
    for item in raw:
        text = item.get("text", "")
        row = build_row(
            source_url=source_url,
            base_url=base_url,
            name="",
            href=item.get("href"),
            text=text,
        )
        if row:
            rows.append(row)
        if limit and len(rows) >= limit:
            break
    return dedupe_rows(rows)


async def scrape(
    urls: list[str],
    out_dir: Path,
    db_path: Path,
    headless: bool,
    user_agent: str,
    limit: int | None = None,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = os.getenv("BASE_URL", "https://bilgewatermarket.com")
    rows: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1440, "height": 1200},
        )
        for url in urls:
            page = await context.new_page()
            try:
                log.info("navigating to %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                await page.wait_for_timeout(NETWORK_SETTLE_MS)
                if is_detail_card_url(url) and not is_cards_index_url(url):
                    row = await extract_detail_row(page, url, base_url)
                    page_rows = [row] if row else []
                else:
                    await load_all_cards(page, limit=limit)
                    page_rows = await extract_candidates(page, url, base_url, limit=limit)
                if limit:
                    remaining = limit - len(rows)
                    if remaining <= 0:
                        break
                    page_rows = page_rows[:remaining]
                rows.extend(page_rows)
            except PlaywrightTimeoutError:
                log.error("timeout loading %s", url)
            except Exception:
                log.exception("failed scraping %s", url)
            finally:
                await page.close()
            if limit and len(rows) >= limit:
                rows = rows[:limit]
                break
        await browser.close()

    rows = dedupe_rows(rows)
    if not rows:
        raise RuntimeError(
            "Scrape returned 0 cards — refusing to overwrite existing data files"
        )
    save(rows, out_dir, db_path)
    return rows


def save(rows: list[dict], out_dir: Path, db_path: Path) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    fields = [
        "collected_at",
        "source_url",
        "name",
        "foil_status",
        "price",
        "url",
        "print_variation",
        "image_url",
        "cn_price",
        "en_price",
        "set_number",
        "raw_text",
    ]
    csv_path = out_dir / f"bilgewater_{stamp}.csv"
    jsonl_path = out_dir / f"bilgewater_{stamp}.jsonl"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    latest_csv_path = out_dir / "bilgewater_latest.csv"
    shutil.copy(csv_path, latest_csv_path)

    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS prices(
        id TEXT PRIMARY KEY,
        collected_at TEXT,
        source_url TEXT,
        name TEXT,
        foil_status TEXT,
        price TEXT,
        url TEXT,
        print_variation TEXT,
        image_url TEXT,
        cn_price TEXT,
        en_price TEXT,
        set_number TEXT,
        raw_text TEXT
    )"""
    )

    # Ensure columns exist for older DB files.
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(prices)").fetchall()}
    for col in ("cn_price", "en_price", "set_number"):
        if col not in existing_cols:
            con.execute(f"ALTER TABLE prices ADD COLUMN {col} TEXT")

    for row in rows:
        con.execute(
            "INSERT OR IGNORE INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [fingerprint(row)] + [row.get(k, "") for k in fields],
        )
    con.commit()
    con.close()

    log.info(
        "saved %d rows to %s, %s, %s, %s",
        len(rows),
        csv_path,
        latest_csv_path,
        jsonl_path,
        db_path,
    )


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Collect Bilgewater Market card data")
    ap.add_argument(
        "--urls",
        default=os.getenv("START_URLS", "https://bilgewatermarket.com/cards"),
        help="Comma-separated URLs to scrape",
    )
    ap.add_argument(
        "--output-dir",
        "--out-dir",
        dest="output_dir",
        default=os.getenv("OUT_DIR", "data"),
        help="Directory for CSV/JSONL output",
    )
    ap.add_argument(
        "--db-path",
        "--db",
        dest="db_path",
        default=os.getenv("SQLITE_PATH", "data/bilgewater.sqlite"),
        help="SQLite database path",
    )
    ap.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run browser headless (default: true)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of cards to collect (for testing)",
    )
    ap.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = ap.parse_args()

    configure_logging(args.verbose)
    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    ua = os.getenv("USER_AGENT", "BilgewaterDailyCollector/1.0")

    rows = asyncio.run(
        scrape(
            urls,
            Path(args.output_dir),
            Path(args.db_path),
            args.headless,
            ua,
            args.limit,
        )
    )
    log.info("collection complete: %d rows", len(rows))


if __name__ == "__main__":
    main()
