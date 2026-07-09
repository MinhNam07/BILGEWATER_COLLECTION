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
DETAIL_WARMUP_MS = 3_000
DEFAULT_CONCURRENCY = 10
DEFAULT_TOTAL_TIMEOUT_SEC = 2_700
DEFAULT_MAX_DETAIL_RETRIES = 1
DEFAULT_MAX_DEBUG_ARTIFACTS = 15
DEFAULT_FAIL_RATE_ABORT = 0.30
DEFAULT_FAIL_RATE_MIN_SAMPLES = 40
PROGRESS_LOG_EVERY = 10

CARDS_LINKS_WAIT_JS = r"""() => {
  const re = /\/cards\/[A-Za-z]+-\d+[A-Za-z]?(?:\/)?$/i;
  for (const a of document.querySelectorAll('a[href*="/cards/"]')) {
    try {
      const u = new URL(a.href || '', location.href);
      if (re.test(u.pathname || '')) return true;
    } catch {}
  }
  return false;
}"""

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

  // Detail pages use h1 for the card name — never list/tile selectors.
  const titleEl = document.querySelector('h1') || document.querySelector('[data-testid="card-name"]');
  let name = norm(titleEl ? titleEl.textContent : '');
  if (!name && document.title) {
    const left = String(document.title).split('|')[0].trim();
    if (left && !/^[A-Z]+-\d+[A-Z]?\b/i.test(left)) name = left;
  }

  // Prefer MARKET PRICES block labels (CN/EN), not OTHER PRINTS / list tiles.
  let cnPrice = null;
  let enPrice = null;
  const headings = [...document.querySelectorAll('p, h2, h3, div, span')];
  let marketRoot = null;
  for (const el of headings) {
    if (/^market\s+prices$/i.test(norm(el.textContent))) {
      marketRoot = el.closest('.rounded-lg') || el.parentElement || el;
      break;
    }
  }
  const priceScope = marketRoot || document;
  for (const label of priceScope.querySelectorAll('span.text-muted-foreground, span.text-xs')) {
    const market = norm(label.textContent).toUpperCase();
    if (market !== 'CN' && market !== 'EN') continue;
    // Sibling / nearby numeric text: "CNY 7.11" or "¥0.53" or bare "7.11".
    const row = label.parentElement || label;
    const rowText = norm(row.textContent || '');
    let m = rowText.match(
      market === 'CN'
        ? /\bCN\b[^0-9]{0,24}(?:CNY|RMB|CN¥|¥|￥)?\s*(\d[\d,]*(?:\.\d+)?)/i
        : /\bEN\b[^0-9]{0,24}(?:USD|US\$|\$)?\s*(\d[\d,]*(?:\.\d+)?)/i
    );
    if (!m) continue;
    if (market === 'CN') cnPrice = m[1];
    if (market === 'EN') enPrice = m[1];
  }

  // Main card art: largest visible non-nav image (prefer /assets/ card art).
  let best = { area: 0, url: '', score: -1 };
  for (const img of document.querySelectorAll('img')) {
    if (!visible(img)) continue;
    const url = img.currentSrc || img.src || '';
    if (!url) continue;
    const low = url.toLowerCase();
    if (low.includes('navbar') || low.includes('logo') || low.includes('favicon')) continue;
    const r = img.getBoundingClientRect();
    const area = r.width * r.height;
    let score = area;
    if (/\/assets\/.*\.(webp|png|jpe?g)/i.test(url)) score += 1e6;
    if (img.closest('[class*="aspect-"]')) score += 5e5;
    if (score > best.score) best = { area, url, score };
  }

  let setNumber = '';
  try {
    const m = (location.pathname || '').match(/\/cards\/([A-Za-z]+-\d+[A-Za-z]?)/i);
    if (m) setNumber = m[1].toUpperCase();
  } catch {}

  let printVariation = 'normal';
  try {
    const pv = (new URL(location.href)).searchParams.get('print_variation');
    if (pv) printVariation = String(pv).toLowerCase();
  } catch {}
  // Badge fallback when URL omits print_variation.
  if (printVariation === 'normal') {
    const badgeText = norm(document.body ? document.body.innerText : '').slice(0, 2500);
    if (/\bshowcase\b/i.test(badgeText)) printVariation = 'showcase';
    else if (/\bfoiled?\b/i.test(badgeText) && !/\bnormal\b/i.test(badgeText)) printVariation = 'foiled';
  }

  const rawText = norm(document.body ? document.body.innerText : '');
  return {
    name,
    cnPrice,
    enPrice,
    imageUrl: best.url || '',
    setNumber,
    printVariation,
    documentTitle: document.title || '',
    // Keep enough body text for Python regex fallback (CN CNY / EN USD).
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
    """Extract CN/EN market price from tile or detail body text.

    Supports symbol style (``CN ¥0.53`` / ``EN $0.04``) and currency-code
    style used on detail pages (``CN CNY 0.53`` / ``EN USD 0.04``), including
    newline-separated variants.
    """
    market = (market or "").strip().upper()
    if market not in {"CN", "EN"}:
        return None
    # Flatten whitespace so "CN\\nCNY\\n7.11" still matches.
    flat = re.sub(r"\s+", " ", text or "")
    if market == "CN":
        patterns = [
            r"(?i)\bCN\s+(?:CNY|RMB|CN¥|¥|￥)\s*(\d[\d,]*(?:\.\d+)?)",
            r"(?i)\bCN\s+(?:US\$|[$¥￥])\s*(\d[\d,]*(?:\.\d+)?)",
        ]
    else:
        patterns = [
            r"(?i)\bEN\s+(?:USD|US\$|\$)\s*(\d[\d,]*(?:\.\d+)?)",
            r"(?i)\bEN\s+(?:US\$|[$¥￥])\s*(\d[\d,]*(?:\.\d+)?)",
        ]
    for pat in patterns:
        m = re.search(pat, flat)
        if not m:
            continue
        try:
            return f"{float(m.group(1).replace(',', '')):.2f}"
        except ValueError:
            continue
    return None


def parse_detail_market_prices(text: str) -> tuple[str | None, str | None]:
    """Prefer prices from the MARKET PRICES section on detail pages."""
    raw = text or ""
    # Prefer the dedicated market block so OTHER PRINTS tiles don't win.
    section = raw
    m = re.search(r"(?i)market\s+prices(.{0,800})", raw, re.S)
    if m:
        section = m.group(1)
    cn = extract_market_price(section, "CN")
    en = extract_market_price(section, "EN")
    if cn is None and en is None and section is not raw:
        cn = extract_market_price(raw, "CN")
        en = extract_market_price(raw, "EN")
    return cn, en


def parse_name_from_document_title(title: str) -> str:
    """Card name from ``Name | ...`` document title when h1 is missing."""
    t = (title or "").strip()
    if not t:
        return ""
    # "Katarina, Reckless Price | Riftbound Card Price History"
    left = t.split("|", 1)[0].strip()
    if not left:
        return ""
    # Skip generic titles like "UNL-023 Riftbound Card Detail | Bilgewater Market"
    if re.match(r"(?i)^[A-Z]+-\d+[A-Z]?\b", left) and " " in left and "Riftbound" in left:
        return ""
    if BAD_NAME_RE.match(left):
        return ""
    return left


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
class BrowserSession:
    """Shared browser state: cached /api/cards-with-prices payload."""

    api_cards: list[dict] | None = None
    _api_index: dict[tuple[str, str], dict] | None = field(default=None, repr=False)

    def set_api_cards(self, cards: list[dict] | None) -> None:
        self.api_cards = cards if cards else None
        self._api_index = None

    def api_index(self) -> dict[tuple[str, str], dict]:
        if self._api_index is None:
            self._api_index = group_api_cards(self.api_cards or [])
        return self._api_index


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


def normalize_short_card_id(raw: str) -> str:
    """Normalize UNL-028A → UNL-028a to match Bilgewater API card_id prefixes."""
    raw = (raw or "").strip()
    m = re.match(r"^([A-Za-z]+)-(\d+)([A-Za-z])?$", raw)
    if not m:
        return raw.upper()
    suffix = (m.group(3) or "").lower()
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}{suffix}"


def api_lookup_key(url: str) -> tuple[str, str] | None:
    short = parse_set_number_from_url(url)
    if not short:
        return None
    return normalize_short_card_id(short), parse_print_variation_from_url(url)


def group_api_cards(cards: list[dict]) -> dict[tuple[str, str], dict]:
    """Group API cards by (short_id, print_variation), merging CN/EN prices."""
    groups: dict[tuple[str, str], dict] = {}
    for card in cards:
        cid = (card.get("card_id") or card.get("id") or "").strip()
        if not cid:
            continue
        short = normalize_short_card_id(cid.split("/")[0])
        pv = (card.get("print_variation") or "normal").strip().lower()
        key = (short, pv)
        group = groups.setdefault(
            key,
            {
                "short_id": short,
                "print_variation": pv,
                "name": "",
                "cn_price": None,
                "en_price": None,
                "card_id": cid,
            },
        )
        lang = (card.get("language") or "").strip().lower()
        name = (card.get("name") or "").strip()
        if lang == "english":
            if name:
                group["name"] = name
            if card.get("price_usd") is not None:
                group["en_price"] = card["price_usd"]
        elif "chinese" in lang:
            if card.get("price_cny") is not None:
                group["cn_price"] = card["price_cny"]
            if name and not group["name"]:
                group["name"] = name
        elif name and not group["name"]:
            group["name"] = name
    return groups


def api_group_to_url(base_url: str, short_id: str, print_variation: str) -> str:
    path = f"/cards/{short_id}"
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if print_variation and print_variation != "normal":
        url += f"?print_variation={print_variation}"
    return url


def row_from_api_group(
    group: dict, *, base_url: str, source_url: str, href: str | None = None
) -> dict | None:
    cn_val = group.get("cn_price")
    en_val = group.get("en_price")
    cn_price = f"¥{float(cn_val):.2f}" if cn_val is not None else ""
    en_price = f"${float(en_val):.2f}" if en_val is not None else ""
    if not cn_price and not en_price:
        return None
    short_id = group["short_id"]
    pv = group["print_variation"]
    abs_url = href or api_group_to_url(base_url, short_id, pv)
    text_parts = [group.get("name") or short_id, group.get("card_id") or short_id]
    if pv == "foiled":
        text_parts.append("Foiled")
    elif pv == "showcase":
        text_parts.append("Showcase")
    if cn_price:
        text_parts.append(f"CN {cn_price}")
    if en_price:
        text_parts.append(f"EN {en_price}")
    return build_row(
        source_url=source_url,
        base_url=base_url,
        name=group.get("name") or "",
        href=abs_url,
        text=" ".join(text_parts),
        cn_price=cn_price or None,
        en_price=en_price or None,
        is_foil=(pv == "foiled"),
        is_showcase=(pv == "showcase"),
        print_variation=pv,
        image_url="",
        set_number=short_id,
    )


def rows_from_api_cards(
    cards: list[dict],
    *,
    base_url: str,
    source_url: str,
    limit: int | None = None,
) -> list[dict]:
    groups = group_api_cards(cards)
    keys = sorted(groups.keys())
    rows: list[dict] = []
    for key in keys:
        row = row_from_api_group(groups[key], base_url=base_url, source_url=source_url)
        if row:
            rows.append(row)
        if limit and len(rows) >= limit:
            break
    return dedupe_rows(rows)


def row_from_api_for_url(
    session: BrowserSession, url: str, base_url: str
) -> dict | None:
    if not session.api_cards:
        return None
    key = api_lookup_key(url)
    if not key:
        return None
    group = session.api_index().get(key)
    if not group:
        return None
    return row_from_api_group(group, base_url=base_url, source_url=url, href=url)


def detail_attempt_urls(url: str, max_retries: int) -> list[str]:
    """Primary URL plus retry variants (case + print_variation=normal)."""
    out: list[str] = [url]
    if max_retries < 1:
        return out
    short = parse_set_number_from_url(url)
    if short:
        normalized = normalize_short_card_id(short)
        if normalized != short:
            try:
                p = urlparse(url)
                path = re.sub(
                    r"/cards/[^/?#]+",
                    f"/cards/{normalized}",
                    p.path or "",
                    count=1,
                )
                variant = p._replace(path=path).geturl()
                if variant.lower() not in {u.lower() for u in out}:
                    out.append(variant)
            except Exception:
                pass
    try:
        p = urlparse(url)
    except Exception:
        return out
    if "print_variation=" not in (p.query or ""):
        sep = "&" if p.query else "?"
        retry = f"{url}{sep}print_variation=normal"
        if retry.lower() not in {u.lower() for u in out}:
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


def is_card_not_found(body_text: str) -> bool:
    return bool(re.search(r"\bcard not found\b", body_text or "", re.I))


def cards_index_url(base_url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", "cards")


async def capture_cards_with_prices(
    page,
    url: str,
    *,
    timeout_ms: int = PAGE_TIMEOUT_MS,
) -> list[dict] | None:
    """Navigate and capture /api/cards-with-prices JSON (primary data source)."""
    async def _read_cards(resp) -> list[dict] | None:
        try:
            data = await resp.json()
            cards = data.get("cards") if isinstance(data, dict) else None
            if isinstance(cards, list) and cards:
                log.info("captured cards-with-prices API: %d entries", len(cards))
                return cards
        except Exception:
            log.exception("failed parsing cards-with-prices response")
        return None

    try:
        async with page.expect_response(
            lambda r: "cards-with-prices" in r.url and r.status == 200,
            timeout=min(timeout_ms, 45_000),
        ) as resp_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return await _read_cards(await resp_info.value)
    except PlaywrightTimeoutError:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            return None
        try:
            async with page.expect_response(
                lambda r: "cards-with-prices" in r.url and r.status == 200,
                timeout=min(timeout_ms, 20_000),
            ) as resp_info:
                await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            return await _read_cards(await resp_info.value)
        except PlaywrightTimeoutError:
            return None
    except Exception:
        log.exception("failed capturing cards-with-prices from %s", url)
        return None


async def warmup_cards_session(
    page,
    base_url: str,
    *,
    timeout_ms: int = PAGE_TIMEOUT_MS,
    session: BrowserSession | None = None,
) -> bool:
    """Load /cards so App Check + API session exist before detail navigation."""
    cards_url = cards_index_url(base_url)
    try:
        api_cards = await capture_cards_with_prices(page, cards_url, timeout_ms=timeout_ms)
        if session is not None and api_cards:
            session.set_api_cards(api_cards)
        if not api_cards:
            try:
                await page.wait_for_function(CARDS_LINKS_WAIT_JS, timeout=min(timeout_ms, 45_000))
            except PlaywrightTimeoutError:
                pass
        await page.wait_for_timeout(DETAIL_WARMUP_MS)
        ready = bool(api_cards) or await page.evaluate(CARDS_LINKS_WAIT_JS)
        if ready:
            log.info("cards session warmup ready: %s (api=%s)", cards_url, bool(api_cards))
        else:
            log.warning("cards session warmup: no API or card links on %s", cards_url)
        return bool(ready)
    except Exception:
        log.exception("cards session warmup failed for %s", cards_url)
        return False


async def ensure_api_cards_loaded(
    context,
    base_url: str,
    session: BrowserSession,
    *,
    attempts: int = 3,
) -> bool:
    """Retry /cards warmup until cards-with-prices is cached in session."""
    if session.api_cards:
        return True
    cards_url = cards_index_url(base_url)
    for attempt in range(attempts):
        page = await context.new_page()
        try:
            api_cards = await capture_cards_with_prices(page, cards_url)
            if api_cards:
                session.set_api_cards(api_cards)
                return True
        finally:
            await page.close()
        if attempt + 1 < attempts:
            log.warning(
                "cards-with-prices not captured (attempt %d/%d) — retrying",
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(2 * (attempt + 1))
    return False


async def warmup_detail_context(
    context, base_url: str, session: BrowserSession | None = None
) -> bool:
    if session is not None:
        return await ensure_api_cards_loaded(context, base_url, session)
    page = await context.new_page()
    try:
        return await warmup_cards_session(page, base_url, session=session)
    finally:
        await page.close()


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
    page,
    out_dir: Path,
    url: str,
    base_url: str,
    stats: ScrapeStats | None = None,
    session: BrowserSession | None = None,
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
        await page.wait_for_function(CARDS_LINKS_WAIT_JS, timeout=45_000)
    except PlaywrightTimeoutError:
        pass

    count = await count_cards(page)
    if not count:
        log.warning("cards list still empty after waits for %s — retrying session warmup", url)
        if await warmup_cards_session(page, base_url, session=session):
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
    text = (item.get("text") or "").strip()
    name = (item.get("name") or "").strip()
    if not name:
        name = parse_name_from_document_title(item.get("documentTitle") or "")

    # DOM may leave cn/en null; always fill from MARKET PRICES text fallback.
    cn_dom = item.get("cnPrice")
    en_dom = item.get("enPrice")
    cn_fb, en_fb = parse_detail_market_prices(text)
    cn_price = normalize_price(cn_dom) or cn_fb
    en_price = normalize_price(en_dom) or en_fb

    image_url = (item.get("imageUrl") or "").strip()
    # Reject obvious chrome images if JS missed filters.
    if image_url and re.search(r"(?i)navbar|favicon|/logo", image_url):
        image_url = ""

    pv = parse_print_variation_from_url(url)
    if pv == "normal":
        js_pv = (item.get("printVariation") or "").strip().lower()
        if js_pv in {"foiled", "showcase", "normal"}:
            pv = js_pv

    set_number = (
        parse_set_number_from_url(url)
        or (item.get("setNumber") or "").strip().upper()
        or ""
    )
    if set_number and not re.match(r"^[A-Z]+-\d{3}[A-Z]?$", set_number):
        # Normalize UNL-23 -> UNL-023 when coming from raw pathname.
        set_number = parse_set_number_from_url(f"/cards/{set_number}") or set_number

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
    session: BrowserSession | None = None,
) -> tuple[dict | None, str, str, bool]:
    """Navigate once and extract a detail row. Returns (row, title, body, blocked)."""

    if session is not None:
        api_row = row_from_api_for_url(session, url, base_url)
        if api_row:
            return api_row, "", "", False

    async def _load_and_extract(target_url: str) -> tuple[dict | None, str, str, bool]:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=detail_timeout_ms)
        await page.wait_for_timeout(DETAIL_SETTLE_MS)
        settle_budget = max(2_000, min(8_000, max(detail_timeout_ms // 2, 2_000)))
        try:
            await page.wait_for_function(
                r"""() => {
                  const t = (document.body && (document.body.innerText || '')) || '';
                  if (/card not found/i.test(t)) return false;
                  if (/market\s+prices/i.test(t)) return true;
                  if (/\bCN\s+CNY\b/i.test(t) && /\bEN\s+USD\b/i.test(t)) return true;
                  if (document.querySelector('h1') && t.length > 140) return true;
                  for (const img of document.querySelectorAll('img')) {
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (!src || src.includes('navbar') || src.includes('logo')) continue;
                    const r = img.getBoundingClientRect();
                    if (r.width >= 120 && r.height >= 120 && /\/assets\//.test(src)) return true;
                  }
                  return false;
                }""",
                timeout=settle_budget,
            )
        except PlaywrightTimeoutError:
            pass

        row = await extract_detail_row(page, target_url, base_url)
        title, body = await page_debug_summary(page)
        blocked = is_blocked_or_challenged(title, body)
        return row, title, body, blocked

    row, title, body, blocked = await _load_and_extract(url)
    if not row and not blocked and is_card_not_found(body):
        log.info("detail page not ready for %s — warming /cards session and retrying", url)
        if await warmup_cards_session(page, base_url, session=session):
            if session is not None:
                api_row = row_from_api_for_url(session, url, base_url)
                if api_row:
                    return api_row, title, body, blocked
            row, title, body, blocked = await _load_and_extract(url)
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
    session: BrowserSession | None = None,
) -> list[dict]:
    """Concurrent detail scrape with deadline, fail-rate abort, and debug cap."""
    if not urls:
        return []

    await warmup_detail_context(context, base_url, session=session)

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

            row = None
            blocked = False
            last_title = ""
            last_body = ""
            tried: list[str] = []
            page = None

            if session is not None and session.api_cards:
                for attempt_url in detail_attempt_urls(url, max_retries):
                    tried.append(attempt_url)
                    row = row_from_api_for_url(session, attempt_url, base_url)
                    if row:
                        row["source_url"] = url
                        break

            if not row:
                page = await context.new_page()
                try:
                    for attempt_url in detail_attempt_urls(url, max_retries):
                        if stop_event.is_set():
                            break
                        if attempt_url in tried:
                            continue
                        check_deadline(stats.t0, total_timeout_sec)
                        tried.append(attempt_url)
                        try:
                            row, last_title, last_body, blocked = await fetch_detail_once(
                                page,
                                attempt_url,
                                base_url,
                                detail_timeout_ms,
                                session=session,
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

                    if not row and not stop_event.is_set() and page is not None:
                        reason = (
                            "blocked_or_challenged"
                            if blocked
                            else "card_not_found"
                            if is_card_not_found(last_body)
                            else "detail_empty"
                        )
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
                    if page is not None:
                        try:
                            await save_debug_artifacts(page, out_dir, "exception", stats)
                        except Exception:
                            pass
                    row = None
                finally:
                    if page is not None:
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
                reason = (
                    "blocked_or_challenged"
                    if blocked
                    else "card_not_found"
                    if is_card_not_found(last_body)
                    else "detail_empty"
                )
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
    session: BrowserSession | None = None,
) -> tuple[list[dict], list[str]]:
    """Scroll/extract /cards list. Returns (complete_rows, incomplete_detail_urls)."""
    check_deadline(stats.t0, total_timeout_sec)
    page = await context.new_page()
    incomplete_urls: list[str] = []
    try:
        log.info("navigating to %s", url)
        api_cards = await capture_cards_with_prices(page, url, timeout_ms=PAGE_TIMEOUT_MS)
        if session is not None and api_cards:
            session.set_api_cards(api_cards)
        if api_cards:
            api_rows = rows_from_api_cards(
                api_cards, base_url=base_url, source_url=url, limit=limit
            )
            if api_rows:
                log.info("list extract via API: %d rows from %s", len(api_rows), url)
                return api_rows, []

        await page.wait_for_timeout(NETWORK_SETTLE_MS)
        visible = await ensure_cards_list_ready(
            page, out_dir, url, base_url, stats, session=session
        )
        check_deadline(stats.t0, total_timeout_sec)
        if visible == 0:
            if session is not None and session.api_cards:
                api_rows = rows_from_api_cards(
                    session.api_cards,
                    base_url=base_url,
                    source_url=url,
                    limit=limit,
                )
                if api_rows:
                    log.info(
                        "list extract via cached API after empty DOM: %d rows from %s",
                        len(api_rows),
                        url,
                    )
                    return api_rows, []
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
    session = BrowserSession()

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
                    session=session,
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
                        session=session,
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
                                session=session,
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
                        if session.api_cards:
                            api_rows = rows_from_api_cards(
                                session.api_cards,
                                base_url=base_url,
                                source_url=list_url,
                                limit=limit,
                            )
                            if api_rows:
                                log.warning(
                                    "0 cards in DOM on %s; using %d rows from cards-with-prices API",
                                    list_url,
                                    len(api_rows),
                                )
                                rows = api_rows
                                stats.ok = len(api_rows)
                                stats.total = len(api_rows)
                                stats.processed = 0
                                break

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
                                session=session,
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
