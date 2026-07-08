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
from collections import deque
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
CARD_TILE_SELECTOR = 'a.block[href*="/cards/"]'
CARD_LINK_SELECTOR = 'a[href*="/cards/"]'
CARD_DETAIL_HREF_RE = re.compile(r"/cards/[A-Za-z]+-\d+[A-Za-z]?(?:/)?$", re.I)
SCROLL_DELAY_MS = 800
SCROLL_STEP_PX = 2500
STABLE_ROUNDS = 3
MAX_SCROLL_ROUNDS = 100
PAGE_TIMEOUT_MS = 60_000
NETWORK_SETTLE_MS = 2_000
HYDRATION_WAIT_MS = 10_000

EXTRACT_CARDS_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const isCardDetail = (href) => {
    try {
      const u = new URL(href, location.href);
      return /\/cards\/[A-Za-z]+-\d+[A-Za-z]?(?:\/)?$/i.test(u.pathname || '');
    } catch {
      return false;
    }
  };

  const cards = [];
  // Primary selector is best when present, but fall back to any /cards/ link.
  const primary = [...document.querySelectorAll('a.block[href*="/cards/"]')];
  const broad = [...document.querySelectorAll('a[href*="/cards/"]')];
  const anchors = primary.length ? primary : broad;

  for (const anchor of anchors) {
    if (!visible(anchor)) continue;

    const h3 = anchor.querySelector('h3');
    const name = h3 ? h3.innerText.trim() : '';
    const href = anchor.href;
    if (!href || !isCardDetail(href)) continue;
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
    // Some pages render market/price sections far below; keep enough text
    // for robust price parsing, while still bounding payload size.
    text: rawText.slice(0, 15000),
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
    # Count likely detail links, not a fragile CSS class selector.
    js = r"""
    () => {
      const re = /\/cards\/[A-Za-z]+-\d+[A-Za-z]?(?:\/)?$/i;
      const out = [];
      for (const a of document.querySelectorAll('a[href*="/cards/"]')) {
        const href = a.href || '';
        try {
          const u = new URL(href, location.href);
          if (!re.test(u.pathname || '')) continue;
        } catch { continue; }
        out.push(a);
      }
      return out.length;
    }
    """
    return await page.evaluate(js)


async def save_debug_artifacts(page, out_dir: Path, slug: str) -> None:
    try:
        dbg = out_dir / "debug"
        dbg.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", slug)[:80]
        png = dbg / f"{ts}_{safe}.png"
        html = dbg / f"{ts}_{safe}.html"
        await page.screenshot(path=str(png), full_page=True)
        html.write_text(await page.content(), encoding="utf-8")
        log.info("wrote debug artifacts: %s, %s", png, html)
    except Exception:
        log.exception("failed writing debug artifacts for %s", slug)


async def page_debug_summary(page) -> tuple[str, str]:
    try:
        title = (await page.title()) or ""
    except Exception:
        title = ""
    try:
        body_text = await page.evaluate(
            r"""() => (document.body && (document.body.innerText || '')) || ''"""
        )
    except Exception:
        body_text = ""
    body_text = re.sub(r"\s+", " ", (body_text or "")).strip()
    return title.strip(), body_text


def is_blocked_or_challenged(title: str, body_text: str) -> bool:
    t = (title or "").lower()
    b = (body_text or "").lower()
    signals = [
        "just a moment",
        "attention required",
        "cloudflare",
        "checking your browser",
        "verify you are human",
        "please enable cookies",
        "access denied",
        "security check",
        "challenge",
        "cf-chl",
        "turnstile",
    ]
    return any(s in t for s in signals) or any(s in b for s in signals)


def generate_detail_url_variants(url: str) -> list[str]:
    """Try canonical variants before declaring detail scrape failed."""
    out: list[str] = []
    try:
        p = urlparse(url)
    except Exception:
        return [url]

    path = p.path or ""
    m = re.search(r"(/cards/)([^/?#]+)", path, re.I)
    if not m:
        return [url]

    prefix = m.group(1)
    slug = m.group(2)
    base = f"{p.scheme}://{p.netloc}"
    query = f"?{p.query}" if p.query else ""

    def add(u: str) -> None:
        if u.lower() not in {x.lower() for x in out}:
            out.append(u)

    # 1) original URL
    add(url)
    # 2) lowercase slug
    add(f"{base}{prefix}{slug.lower()}{query}")
    # 3) uppercase slug
    add(f"{base}{prefix}{slug.upper()}{query}")
    # 4) if no print_variation, try explicit normal
    if "print_variation=" not in (p.query or ""):
        sep = "&" if p.query else "?"
        add(f"{base}{prefix}{slug}{query}{sep}print_variation=normal")
        add(f"{base}{prefix}{slug.lower()}{query}{sep}print_variation=normal")
        add(f"{base}{prefix}{slug.upper()}{query}{sep}print_variation=normal")
    return out


def load_existing_cards_index(path: Path) -> dict[str, dict]:
    """Index existing web/cards.json entries by lowercased URL (without fragment)."""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        cards = data.get("cards") or []
        out: dict[str, dict] = {}
        for c in cards:
            u = (c.get("url") or "").strip()
            if not u:
                continue
            try:
                pu = urlparse(u)
                key = pu._replace(fragment="").geturl().lower()
            except Exception:
                key = u.lower()
            out[key] = c
        return out
    except Exception:
        log.exception("failed reading existing cards index from %s", path)
        return {}


async def ensure_cards_list_ready(page, out_dir: Path, url: str) -> int:
    # Prefer condition-based waiting over fixed sleeps, but keep bounded waits
    # to avoid hanging in CI.
    count = await count_cards(page)
    if count:
        return count

    # First, wait for client hydration/network to settle.
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(HYDRATION_WAIT_MS)
    count = await count_cards(page)
    if count:
        return count

    # Then, wait for any /cards/ anchors to appear (broader than tile selector).
    try:
        await page.wait_for_function(
            r"""() => {
              const re = /\/cards\/[A-Za-z]+-\d+[A-Za-z]?(?:\/)?$/i;
              for (const a of document.querySelectorAll('a[href*="/cards/"]')) {
                try {
                  const u = new URL(a.href || '', location.href);
                  if (re.test(u.pathname || '')) return true;
                } catch {}
              }
              return false;
            }""",
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        pass

    count = await count_cards(page)
    if not count:
        log.warning("cards list still empty after waits for %s", url)
        await save_debug_artifacts(page, out_dir, "cards_list_empty")
    return count


def seed_detail_urls_from_cards_json(path: Path) -> list[str]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        cards = data.get("cards") or []
        urls = []
        for c in cards:
            u = (c.get("url") or "").strip()
            if u:
                urls.append(u)
        # Stable order; de-dupe case-insensitively.
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            k = u.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(u)
        return out
    except Exception:
        log.exception("failed reading seed urls from %s", path)
        return []


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
    seeded_full_scrape = False
    existing_cards_by_url = load_existing_cards_index(Path("web/cards.json"))
    targeted_attempts: list[dict] = []

    async with async_playwright() as p:
        launch_args: list[str] = []
        # New headless mode is more stable; some Chromium builds crash with old mode.
        if headless:
            launch_args.append("--headless=new")
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1440, "height": 1200},
        )
        queue: deque[str] = deque(urls)
        while queue:
            url = queue.popleft()
            page = await context.new_page()
            try:
                log.info("navigating to %s", url)
                if is_detail_card_url(url) and not is_cards_index_url(url):
                    # Targeted detail: try URL variants before declaring failure.
                    variants = generate_detail_url_variants(url)
                    row = None
                    blocked = False
                    last_title = ""
                    last_body = ""
                    tried: list[str] = []

                    for v in variants:
                        tried.append(v)
                        await page.goto(v, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                        await page.wait_for_timeout(NETWORK_SETTLE_MS)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=30_000)
                        except PlaywrightTimeoutError:
                            pass
                        await page.wait_for_timeout(3_000)
                        try:
                            await page.wait_for_function(
                                r"""() => {
                                  const t = (document.body && (document.body.innerText || '')) || '';
                                  return /(CN¥|US\$|SGD|USD|CNY|RMB|[$¥￥€£])\s*\d/i.test(t) || /\b(EN|CN)\s+(USD|CNY)\b/i.test(t);
                                }""",
                                timeout=20_000,
                            )
                        except PlaywrightTimeoutError:
                            pass

                        row = await extract_detail_row(page, v, base_url)
                        if row:
                            # Keep the original source_url for reporting/merge stability.
                            row["source_url"] = url
                            break

                        last_title, last_body = await page_debug_summary(page)
                        blocked = is_blocked_or_challenged(last_title, last_body)
                        if blocked:
                            # No point retrying more variants if we're challenged.
                            break

                    if not row:
                        reason = "blocked_or_challenged" if blocked else "detail_empty"
                        log.warning(
                            "%s: 0 row for %s (tried=%d) title=%r body[0:500]=%r",
                            reason,
                            url,
                            len(tried),
                            last_title,
                            (last_body or "")[:500],
                        )
                        await save_debug_artifacts(page, out_dir, f"{reason}_{parse_set_number_from_url(url) or 'detail'}")

                        # If this card already exists in catalog, do not treat as deletion-worthy.
                        try:
                            key = urlparse(url)._replace(fragment="").geturl().lower()
                        except Exception:
                            key = url.lower()
                        if key in existing_cards_by_url:
                            log.warning("skipped_detail_failed: keeping existing entry for %s", url)

                        targeted_attempts.append(
                            {
                                "url": url,
                                "status": reason,
                                "tried": tried,
                                "title": last_title,
                                "body_500": (last_body or "")[:500],
                            }
                        )
                        page_rows = []
                    else:
                        targeted_attempts.append({"url": url, "status": "ok", "final_url": row.get("url", "")})
                        page_rows = [row]
                else:
                    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                    await page.wait_for_timeout(NETWORK_SETTLE_MS)
                    # /cards list page (SPA). Do not depend on a single selector.
                    visible = await ensure_cards_list_ready(page, out_dir, url)
                    if visible == 0 and is_cards_index_url(url) and not seeded_full_scrape:
                        # Runner sometimes renders 0 list tiles; fall back to existing catalog seed.
                        seed_path = Path("web/cards.json")
                        seed_urls = seed_detail_urls_from_cards_json(seed_path)
                        if seed_urls:
                            seeded_full_scrape = True
                            log.warning(
                                "0 cards discovered on %s; seeding %d detail URLs from %s",
                                url,
                                len(seed_urls),
                                seed_path,
                            )
                            await save_debug_artifacts(page, out_dir, "cards_list_seeded")
                            # Refresh detail pages in batches by pushing into the queue.
                            for u in seed_urls:
                                queue.append(u)
                            page_rows = []
                        else:
                            log.error(
                                "0 cards discovered on %s and no seed available at %s",
                                url,
                                seed_path,
                            )
                            await save_debug_artifacts(page, out_dir, "cards_list_no_seed")
                            page_rows = []
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
                await save_debug_artifacts(page, out_dir, "timeout")
            except Exception:
                log.exception("failed scraping %s", url)
                await save_debug_artifacts(page, out_dir, "exception")
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

    # Targeted runs should report failures clearly in logs.
    if targeted_attempts:
        ok = sum(1 for a in targeted_attempts if a.get("status") == "ok")
        failed = [a for a in targeted_attempts if a.get("status") != "ok"]
        log.info("targeted_summary: ok=%d failed=%d", ok, len(failed))
        for a in failed:
            log.warning(
                "targeted_failed: status=%s url=%s title=%r body[0:200]=%r",
                a.get("status"),
                a.get("url"),
                a.get("title", ""),
                (a.get("body_500") or "")[:200],
            )
        if ok < 1:
            raise RuntimeError("Targeted scrape produced 0 successful detail rows")

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
    expected_cols = ["collected_at", "source_url", "name", "foil_status", "price", "url", "print_variation", "image_url", "cn_price", "en_price", "set_number", "raw_text"]
    for col in expected_cols:
        if col not in existing_cols:
            con.execute(f"ALTER TABLE prices ADD COLUMN {col} TEXT")

    for row in rows:
        cols_sql = ",".join(["id"] + expected_cols)
        placeholders = ",".join(["?"] * (1 + len(expected_cols)))
        con.execute(
            f"INSERT OR IGNORE INTO prices ({cols_sql}) VALUES ({placeholders})",
            [fingerprint(row)] + [row.get(k, "") for k in expected_cols],
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
