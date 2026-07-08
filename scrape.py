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
import time
from dataclasses import dataclass, field
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
DETAIL_TIMEOUT_MS = 12_000
DETAIL_SETTLE_MS = 800
DEFAULT_CONCURRENCY = 10
DEFAULT_TOTAL_TIMEOUT_SEC = 2_700
DEFAULT_MAX_DETAIL_RETRIES = 1
DEFAULT_MAX_DEBUG_ARTIFACTS = 15
DEFAULT_FAIL_RATE_ABORT = 0.30
DEFAULT_FAIL_RATE_MIN_SAMPLES = 40
PROGRESS_LOG_EVERY = 10

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


@dataclass
class ScrapeStats:
    total: int = 0
    processed: int = 0
    ok: int = 0
    failed: int = 0
    skipped_existing: int = 0
    t0: float = field(default_factory=time.monotonic)
    debug_written: int = 0
    max_debug_artifacts: int = DEFAULT_MAX_DEBUG_ARTIFACTS
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    abort_requested: bool = False
    abort_reason: str = ""

    def elapsed_sec(self) -> float:
        return time.monotonic() - self.t0

    def log_progress(self, *, force: bool = False) -> None:
        if not force and self.processed > 0 and self.processed % PROGRESS_LOG_EVERY != 0:
            return
        total = self.total if self.total else "?"
        log.info(
            "progress: processed=%s/%s ok=%d failed=%d skipped_existing=%d elapsed=%.0fs",
            self.processed,
            total,
            self.ok,
            self.failed,
            self.skipped_existing,
            self.elapsed_sec(),
        )


class FullScrapeAbort(RuntimeError):
    """Raised when full scrape should stop without saving (timeout / fail-rate)."""


def deadline_exceeded(t0: float, total_timeout_sec: int | None) -> bool:
    if not total_timeout_sec or total_timeout_sec <= 0:
        return False
    return (time.monotonic() - t0) >= total_timeout_sec


def check_deadline(t0: float, total_timeout_sec: int | None) -> None:
    if deadline_exceeded(t0, total_timeout_sec):
        raise FullScrapeAbort(
            f"Full scrape exceeded total timeout of {total_timeout_sec}s — refusing to save"
        )


def url_index_key(url: str) -> str:
    try:
        return urlparse(url)._replace(fragment="").geturl().lower()
    except Exception:
        return url.lower()


def detail_attempt_urls(url: str, max_retries: int) -> list[str]:
    """Primary URL plus at most one retry variant (print_variation=normal)."""
    out: list[str] = [url]
    if max_retries < 1:
        return out
    try:
        p = urlparse(url)
    except Exception:
        return out
    if "print_variation=" in (p.query or ""):
        return out
    sep = "&" if p.query else "?"
    retry = f"{url}{sep}print_variation=normal"
    if retry.lower() != url.lower():
        out.append(retry)
    return out


def row_needs_detail_enrich(row: dict) -> bool:
    price = (row.get("price") or "").strip()
    image = (row.get("image_url") or "").strip()
    return not price or not image


async def save_debug_artifacts(
    page,
    out_dir: Path,
    slug: str,
    stats: ScrapeStats | None = None,
) -> None:
    if stats is not None:
        async with stats._lock:
            if stats.debug_written >= stats.max_debug_artifacts:
                log.warning(
                    "skipping debug artifact for %s (cap %d reached)",
                    slug,
                    stats.max_debug_artifacts,
                )
                return
            stats.debug_written += 1
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


async def ensure_cards_list_ready(
    page, out_dir: Path, url: str, stats: ScrapeStats | None = None
) -> int:
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
        await save_debug_artifacts(page, out_dir, "cards_list_empty", stats)
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


async def fetch_detail_once(
    page,
    url: str,
    base_url: str,
    detail_timeout_ms: int,
) -> tuple[dict | None, str, str, bool]:
    """Navigate once and extract a detail row. Returns (row, title, body, blocked)."""
    await page.goto(url, wait_until="domcontentloaded", timeout=detail_timeout_ms)
    await page.wait_for_timeout(DETAIL_SETTLE_MS)
    settle_budget = max(1_000, min(4_000, detail_timeout_ms // 3))
    try:
        await page.wait_for_function(
            r"""() => {
              const t = (document.body && (document.body.innerText || '')) || '';
              return /(CN¥|US\$|SGD|USD|CNY|RMB|[$¥￥€£])\s*\d/i.test(t)
                || /\b(EN|CN)\s+(USD|CNY)\b/i.test(t)
                || !!document.querySelector('h1');
            }""",
            timeout=settle_budget,
        )
    except PlaywrightTimeoutError:
        pass

    row = await extract_detail_row(page, url, base_url)
    title, body = await page_debug_summary(page)
    blocked = is_blocked_or_challenged(title, body)
    return row, title, body, blocked


async def scrape_detail_urls(
    context,
    urls: list[str],
    *,
    base_url: str,
    out_dir: Path,
    stats: ScrapeStats,
    existing_cards_by_url: dict[str, dict],
    concurrency: int,
    detail_timeout_ms: int,
    max_retries: int,
    limit: int | None,
    total_timeout_sec: int | None,
    fail_rate_abort: float | None,
    fail_rate_min_samples: int,
    record_attempts: list[dict] | None = None,
) -> list[dict]:
    """Concurrent detail scrape with deadline, fail-rate abort, and debug cap."""
    if not urls:
        return []

    work_urls = urls[:limit] if limit else list(urls)
    stats.total = len(work_urls)
    sem = asyncio.Semaphore(max(1, concurrency))
    rows: list[dict] = []
    rows_lock = asyncio.Lock()
    stop_event = asyncio.Event()

    async def maybe_abort_fail_rate() -> None:
        if fail_rate_abort is None or fail_rate_abort <= 0:
            return
        decided = stats.ok + stats.failed
        if decided < fail_rate_min_samples:
            return
        rate = stats.failed / decided if decided else 0.0
        if rate > fail_rate_abort:
            reason = (
                f"Detail fail rate {rate:.0%} > {fail_rate_abort:.0%} "
                f"after {decided} attempts — aborting (no save)"
            )
            stats.abort_requested = True
            stats.abort_reason = reason
            stop_event.set()
            raise FullScrapeAbort(reason)

    async def worker(url: str) -> None:
        if stop_event.is_set():
            return
        check_deadline(stats.t0, total_timeout_sec)
        if limit is not None:
            async with rows_lock:
                if len(rows) >= limit:
                    stop_event.set()
                    return

        async with sem:
            if stop_event.is_set():
                return
            check_deadline(stats.t0, total_timeout_sec)

            page = await context.new_page()
            row = None
            blocked = False
            last_title = ""
            last_body = ""
            tried: list[str] = []
            try:
                for attempt_url in detail_attempt_urls(url, max_retries):
                    if stop_event.is_set():
                        break
                    check_deadline(stats.t0, total_timeout_sec)
                    tried.append(attempt_url)
                    try:
                        row, last_title, last_body, blocked = await fetch_detail_once(
                            page, attempt_url, base_url, detail_timeout_ms
                        )
                    except PlaywrightTimeoutError:
                        last_title, last_body = await page_debug_summary(page)
                        blocked = is_blocked_or_challenged(last_title, last_body)
                        row = None
                        await save_debug_artifacts(
                            page,
                            out_dir,
                            f"timeout_{parse_set_number_from_url(url) or 'detail'}",
                            stats,
                        )
                    if row:
                        row["source_url"] = url
                        break
                    if blocked:
                        break

                if not row and not stop_event.is_set():
                    reason = "blocked_or_challenged" if blocked else "detail_empty"
                    await save_debug_artifacts(
                        page,
                        out_dir,
                        f"{reason}_{parse_set_number_from_url(url) or 'detail'}",
                        stats,
                    )
            except FullScrapeAbort:
                raise
            except Exception:
                log.exception("failed scraping detail %s", url)
                try:
                    await save_debug_artifacts(page, out_dir, "exception", stats)
                except Exception:
                    pass
                row = None
            finally:
                await page.close()

            async with stats._lock:
                stats.processed += 1
                if row:
                    stats.ok += 1
                else:
                    stats.failed += 1
                    key = url_index_key(url)
                    if key in existing_cards_by_url:
                        stats.skipped_existing += 1
                        log.warning(
                            "skipped_detail_failed: keeping existing entry for %s", url
                        )
                stats.log_progress()

            if row:
                async with rows_lock:
                    if limit is None or len(rows) < limit:
                        rows.append(row)
                    if limit is not None and len(rows) >= limit:
                        stop_event.set()
                if record_attempts is not None:
                    record_attempts.append(
                        {"url": url, "status": "ok", "final_url": row.get("url", "")}
                    )
            else:
                reason = "blocked_or_challenged" if blocked else "detail_empty"
                log.warning(
                    "%s: 0 row for %s (tried=%d) title=%r body[0:200]=%r",
                    reason,
                    url,
                    len(tried),
                    last_title,
                    (last_body or "")[:200],
                )
                if record_attempts is not None:
                    record_attempts.append(
                        {
                            "url": url,
                            "status": reason,
                            "tried": tried,
                            "title": last_title,
                            "body_500": (last_body or "")[:500],
                        }
                    )
                await maybe_abort_fail_rate()

            check_deadline(stats.t0, total_timeout_sec)

    tasks = [asyncio.create_task(worker(u)) for u in work_urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, FullScrapeAbort):
            raise r
        if isinstance(r, Exception):
            log.warning("detail worker error: %s", r)

    if stats.abort_requested and stats.abort_reason:
        raise FullScrapeAbort(stats.abort_reason)

    check_deadline(stats.t0, total_timeout_sec)
    stats.log_progress(force=True)
    return rows[:limit] if limit else rows


async def scrape_list_page(
    context,
    url: str,
    *,
    base_url: str,
    out_dir: Path,
    stats: ScrapeStats,
    limit: int | None,
    total_timeout_sec: int | None,
) -> tuple[list[dict], list[str]]:
    """Scroll/extract /cards list. Returns (complete_rows, incomplete_detail_urls)."""
    check_deadline(stats.t0, total_timeout_sec)
    page = await context.new_page()
    incomplete_urls: list[str] = []
    try:
        log.info("navigating to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(NETWORK_SETTLE_MS)
        visible = await ensure_cards_list_ready(page, out_dir, url, stats)
        check_deadline(stats.t0, total_timeout_sec)
        if visible == 0:
            return [], []

        await load_all_cards(page, limit=limit)
        check_deadline(stats.t0, total_timeout_sec)

        # Extract raw tiles to find incomplete (missing price/image) before build_row drops them.
        raw = await page.evaluate(EXTRACT_CARDS_JS)
        rows: list[dict] = []
        for item in raw:
            href = (item.get("href") or "").strip()
            row = build_row(
                source_url=url,
                base_url=base_url,
                name=item.get("name", ""),
                href=href or None,
                text=item.get("text", ""),
                cn_price=item.get("cnPrice"),
                en_price=item.get("enPrice"),
                is_foil=item.get("isFoil"),
                is_showcase=item.get("isShowcase", False),
                print_variation=item.get("printVariation"),
                image_url=item.get("imageUrl"),
            )
            if row and not row_needs_detail_enrich(row):
                rows.append(row)
            elif href and is_detail_card_url(href):
                incomplete_urls.append(href)
            if limit and len(rows) + len(incomplete_urls) >= limit:
                break

        if not rows and not incomplete_urls:
            log.warning("DOM extraction empty for %s, using regex fallback", url)
            rows = await extract_candidates_fallback(page, url, base_url, limit)
            incomplete_urls = [r["url"] for r in rows if row_needs_detail_enrich(r)]
            rows = [r for r in rows if not row_needs_detail_enrich(r)]

        rows = dedupe_rows(rows)
        # Dedupe incomplete URLs
        seen: set[str] = set()
        uniq_incomplete: list[str] = []
        for u in incomplete_urls:
            k = u.lower()
            if k in seen:
                continue
            seen.add(k)
            uniq_incomplete.append(u)

        if limit:
            remaining = max(0, limit - len(rows))
            uniq_incomplete = uniq_incomplete[:remaining]
            rows = rows[:limit]

        log.info(
            "list extract: complete=%d incomplete=%d from %s",
            len(rows),
            len(uniq_incomplete),
            url,
        )
        return rows, uniq_incomplete
    except FullScrapeAbort:
        raise
    except PlaywrightTimeoutError:
        log.error("timeout loading list %s", url)
        await save_debug_artifacts(page, out_dir, "timeout", stats)
        return [], []
    except Exception:
        log.exception("failed scraping list %s", url)
        await save_debug_artifacts(page, out_dir, "exception", stats)
        return [], []
    finally:
        await page.close()


async def scrape(
    urls: list[str],
    out_dir: Path,
    db_path: Path,
    headless: bool,
    user_agent: str,
    limit: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    detail_timeout_ms: int = DETAIL_TIMEOUT_MS,
    total_timeout_sec: int | None = DEFAULT_TOTAL_TIMEOUT_SEC,
    max_detail_retries: int = DEFAULT_MAX_DETAIL_RETRIES,
    max_debug_artifacts: int = DEFAULT_MAX_DEBUG_ARTIFACTS,
    fail_rate_abort: float = DEFAULT_FAIL_RATE_ABORT,
    fail_rate_min_samples: int = DEFAULT_FAIL_RATE_MIN_SAMPLES,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = os.getenv("BASE_URL", "https://bilgewatermarket.com")
    existing_cards_by_url = load_existing_cards_index(Path("web/cards.json"))

    detail_only = all(is_detail_card_url(u) and not is_cards_index_url(u) for u in urls)

    stats = ScrapeStats(max_debug_artifacts=max_debug_artifacts)
    targeted_attempts: list[dict] = []
    rows: list[dict] = []

    async with async_playwright() as p:
        launch_args: list[str] = []
        if headless:
            launch_args.append("--headless=new")
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1440, "height": 1200},
        )
        try:
            if detail_only:
                log.info(
                    "targeted detail scrape: %d url(s) concurrency=%d detail_timeout_ms=%d",
                    len(urls),
                    concurrency,
                    detail_timeout_ms,
                )
                rows = await scrape_detail_urls(
                    context,
                    urls,
                    base_url=base_url,
                    out_dir=out_dir,
                    stats=stats,
                    existing_cards_by_url=existing_cards_by_url,
                    concurrency=concurrency,
                    detail_timeout_ms=detail_timeout_ms,
                    max_retries=max_detail_retries,
                    limit=limit,
                    total_timeout_sec=total_timeout_sec,
                    fail_rate_abort=None,  # no circuit breaker for targeted
                    fail_rate_min_samples=fail_rate_min_samples,
                    record_attempts=targeted_attempts,
                )
            else:
                # Full refresh: prefer /cards list, detail only for gaps or seed fallback.
                list_urls = [u for u in urls if is_cards_index_url(u) or not is_detail_card_url(u)]
                extra_details = [u for u in urls if is_detail_card_url(u) and not is_cards_index_url(u)]
                seeded = False

                for list_url in list_urls or ["https://bilgewatermarket.com/cards"]:
                    check_deadline(stats.t0, total_timeout_sec)
                    page_rows, incomplete = await scrape_list_page(
                        context,
                        list_url,
                        base_url=base_url,
                        out_dir=out_dir,
                        stats=stats,
                        limit=limit,
                        total_timeout_sec=total_timeout_sec,
                    )
                    if page_rows or incomplete:
                        rows.extend(page_rows)
                        enrich_urls = incomplete + extra_details
                        if enrich_urls:
                            remaining = None if limit is None else max(0, limit - len(rows))
                            if remaining == 0:
                                break
                            log.info(
                                "enriching %d incomplete/extra detail URL(s)",
                                len(enrich_urls[:remaining] if remaining else enrich_urls),
                            )
                            enrich_stats = ScrapeStats(
                                max_debug_artifacts=max(
                                    0, max_debug_artifacts - stats.debug_written
                                ),
                                t0=stats.t0,
                            )
                            enriched = await scrape_detail_urls(
                                context,
                                enrich_urls,
                                base_url=base_url,
                                out_dir=out_dir,
                                stats=enrich_stats,
                                existing_cards_by_url=existing_cards_by_url,
                                concurrency=concurrency,
                                detail_timeout_ms=detail_timeout_ms,
                                max_retries=max_detail_retries,
                                limit=remaining,
                                total_timeout_sec=total_timeout_sec,
                                fail_rate_abort=None,
                                fail_rate_min_samples=fail_rate_min_samples,
                            )
                            stats.debug_written += enrich_stats.debug_written
                            stats.ok += enrich_stats.ok
                            stats.failed += enrich_stats.failed
                            stats.skipped_existing += enrich_stats.skipped_existing
                            stats.processed += enrich_stats.processed
                            rows.extend(enriched)
                        break

                    if is_cards_index_url(list_url) and not seeded:
                        seed_path = Path("web/cards.json")
                        seed_urls = seed_detail_urls_from_cards_json(seed_path)
                        if seed_urls:
                            seeded = True
                            if limit:
                                seed_urls = seed_urls[:limit]
                            log.warning(
                                "0 cards discovered on %s; seeding %d detail URLs from %s "
                                "(concurrency=%d detail_timeout_ms=%d)",
                                list_url,
                                len(seed_urls),
                                seed_path,
                                concurrency,
                                detail_timeout_ms,
                            )
                            # cards_list_empty debug already written by ensure_cards_list_ready

                            seed_stats = ScrapeStats(
                                max_debug_artifacts=max(
                                    0, max_debug_artifacts - stats.debug_written
                                ),
                                t0=stats.t0,
                            )
                            rows = await scrape_detail_urls(
                                context,
                                seed_urls,
                                base_url=base_url,
                                out_dir=out_dir,
                                stats=seed_stats,
                                existing_cards_by_url=existing_cards_by_url,
                                concurrency=concurrency,
                                detail_timeout_ms=detail_timeout_ms,
                                max_retries=max_detail_retries,
                                limit=limit,
                                total_timeout_sec=total_timeout_sec,
                                fail_rate_abort=fail_rate_abort,
                                fail_rate_min_samples=fail_rate_min_samples,
                            )
                            stats.ok = seed_stats.ok
                            stats.failed = seed_stats.failed
                            stats.skipped_existing = seed_stats.skipped_existing
                            stats.processed = seed_stats.processed
                            stats.debug_written += seed_stats.debug_written
                            stats.total = seed_stats.total
                        else:
                            log.error(
                                "0 cards discovered on %s and no seed available at %s",
                                list_url,
                                seed_path,
                            )
                    break
        finally:
            await browser.close()

    stats.log_progress(force=True)
    rows = dedupe_rows(rows)
    if limit:
        rows = rows[:limit]

    if not rows:
        raise RuntimeError(
            "Scrape returned 0 cards — refusing to overwrite existing data files"
        )

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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


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
    ap.add_argument(
        "--concurrency",
        type=int,
        default=_env_int("SCRAPE_CONCURRENCY", DEFAULT_CONCURRENCY),
        help=f"Concurrent detail pages (default: {DEFAULT_CONCURRENCY})",
    )
    ap.add_argument(
        "--detail-timeout-ms",
        type=int,
        default=_env_int("DETAIL_TIMEOUT_MS", DETAIL_TIMEOUT_MS),
        help=f"Per-detail page timeout in ms (default: {DETAIL_TIMEOUT_MS})",
    )
    ap.add_argument(
        "--total-timeout-sec",
        type=int,
        default=None,
        help=(
            f"Full-refresh wall timeout in seconds (default: {DEFAULT_TOTAL_TIMEOUT_SEC}; "
            "0 disables; targeted defaults to disabled)"
        ),
    )
    ap.add_argument(
        "--max-detail-retries",
        type=int,
        default=_env_int("MAX_DETAIL_RETRIES", DEFAULT_MAX_DETAIL_RETRIES),
        help=f"Retries per detail URL after first attempt (default: {DEFAULT_MAX_DETAIL_RETRIES})",
    )
    ap.add_argument(
        "--max-debug-artifacts",
        type=int,
        default=_env_int("MAX_DEBUG_ARTIFACTS", DEFAULT_MAX_DEBUG_ARTIFACTS),
        help=f"Max PNG/HTML debug pairs to write (default: {DEFAULT_MAX_DEBUG_ARTIFACTS})",
    )
    ap.add_argument(
        "--fail-rate-abort",
        type=float,
        default=_env_float("FAIL_RATE_ABORT", DEFAULT_FAIL_RATE_ABORT),
        help=f"Abort seeded full scrape when fail rate exceeds this (default: {DEFAULT_FAIL_RATE_ABORT})",
    )
    ap.add_argument(
        "--fail-rate-min-samples",
        type=int,
        default=_env_int("FAIL_RATE_MIN_SAMPLES", DEFAULT_FAIL_RATE_MIN_SAMPLES),
        help=f"Min ok+failed before fail-rate abort (default: {DEFAULT_FAIL_RATE_MIN_SAMPLES})",
    )
    ap.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = ap.parse_args()

    configure_logging(args.verbose)
    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    ua = os.getenv("USER_AGENT", "BilgewaterDailyCollector/1.0")

    detail_only = all(is_detail_card_url(u) and not is_cards_index_url(u) for u in urls)
    if args.total_timeout_sec is not None:
        total_timeout_sec = args.total_timeout_sec
    elif detail_only:
        total_timeout_sec = 0
    else:
        total_timeout_sec = _env_int("TOTAL_TIMEOUT_SEC", DEFAULT_TOTAL_TIMEOUT_SEC)

    rows = asyncio.run(
        scrape(
            urls,
            Path(args.output_dir),
            Path(args.db_path),
            args.headless,
            ua,
            args.limit,
            concurrency=args.concurrency,
            detail_timeout_ms=args.detail_timeout_ms,
            total_timeout_sec=total_timeout_sec if total_timeout_sec > 0 else None,
            max_detail_retries=args.max_detail_retries,
            max_debug_artifacts=args.max_debug_artifacts,
            fail_rate_abort=args.fail_rate_abort,
            fail_rate_min_samples=args.fail_rate_min_samples,
        )
    )
    log.info("collection complete: %d rows", len(rows))


if __name__ == "__main__":
    main()
