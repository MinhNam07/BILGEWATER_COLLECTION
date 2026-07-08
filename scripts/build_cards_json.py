#!/usr/bin/env python3
"""Merge Bilgewater CSV prices with Riftbound catalog metadata → web/cards.json."""

from __future__ import annotations

import csv
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "bilgewater_latest.csv"
OUT_PATH = ROOT / "web" / "cards.json"

RIFTSCRIBE_API = "https://riftscribe.gg/api/cards"
GIST_CATALOG_URL = (
    "https://gist.githubusercontent.com/OwenMelbz/"
    "e04dadf641cc9b81cb882b4612343112/raw/riftbound_v1_cards.json"
)

SET_NUMBER_RE = re.compile(r"\b([A-Z]+)-(\d+)(?:/(\d+))?\b")
URL_SET_RE = re.compile(r"/cards/([A-Z]+)-(\d+)", re.I)


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "BilgewaterCardTracker/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def fetch_riftscribe_catalog() -> dict[tuple[str, int], dict]:
    """Key: (SET_ID, collector_number) → card metadata."""
    catalog: dict[tuple[str, int], dict] = {}
    offset = 0
    limit = 100

    while True:
        url = f"{RIFTSCRIBE_API}?limit={limit}&offset={offset}"
        try:
            batch = fetch_json(url)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"RiftScribe fetch failed at offset {offset}: {exc}", file=sys.stderr)
            break

        if not isinstance(batch, list) or not batch:
            break

        for card in batch:
            set_id = str(card.get("set_id", "")).upper()
            num = card.get("collector_number")
            if not set_id or num is None:
                continue
            try:
                num_int = int(num)
            except (TypeError, ValueError):
                continue
            catalog[(set_id, num_int)] = {
                "name": card.get("name", ""),
                "card_type": card.get("type", "Unknown"),
                "image_url": card.get("image") or "",
                "image_thumb": (card.get("image_thumb") or {}).get("medium", ""),
            }

        if len(batch) < limit:
            break
        offset += limit

    print(f"RiftScribe catalog: {len(catalog)} cards")
    return catalog


def fetch_gist_catalog() -> dict[str, dict]:
    """Key: publicCode e.g. UNL-001/219 → metadata."""
    catalog: dict[str, dict] = {}
    try:
        data = fetch_json(GIST_CATALOG_URL)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Gist catalog fetch failed: {exc}", file=sys.stderr)
        return catalog

    if not isinstance(data, list):
        return catalog

    for card in data:
        code = card.get("publicCode") or card.get("code")
        if not code:
            continue
        card_types = card.get("cardType") or []
        if card_types and isinstance(card_types[0], dict):
            card_type = card_types[0].get("label", "Unknown")
        else:
            card_type = card.get("type", "Unknown")
        image = ""
        img_obj = card.get("cardImage") or {}
        if isinstance(img_obj, dict):
            image = img_obj.get("url", "")
        catalog[str(code).upper()] = {
            "name": card.get("name", ""),
            "card_type": card_type,
            "image_url": image,
            "image_thumb": image,
        }

    print(f"Gist catalog: {len(catalog)} cards")
    return catalog


def parse_set_info(raw_text: str, url: str) -> tuple[str, int, str]:
    """Return (set_code, collector_number, set_number_display)."""
    for source in (raw_text, url):
        m = SET_NUMBER_RE.search(source or "")
        if m:
            set_code = m.group(1).upper()
            num = int(m.group(2))
            total = m.group(3)
            display = f"{set_code}-{num:03d}/{total}" if total else f"{set_code}-{num:03d}"
            return set_code, num, display

    m = URL_SET_RE.search(url or "")
    if m:
        set_code = m.group(1).upper()
        num = int(m.group(2))
        return set_code, num, f"{set_code}-{num:03d}"

    return "", 0, ""


def make_card_id(set_code: str, num: int, foil_status: str, url: str = "") -> str:
    if set_code and num:
        return f"{set_code}-{num:03d}-{foil_status}"
    return f"url-{abs(hash(url)) % 10_000_000:07d}-{foil_status}"


def enrich_row(
    row: dict,
    riftscribe: dict[tuple[str, int], dict],
    gist: dict[str, dict],
) -> dict:
    raw_text = row.get("raw_text", "")
    url = row.get("url", "")
    foil_status = row.get("foil_status", "unknown")
    set_code, num, set_number = parse_set_info(raw_text, url)

    meta = riftscribe.get((set_code, num), {}) if set_code and num else {}
    if not meta and set_number:
        gist_key = set_number.upper()
        # Try UNL-001/219 and UNL-001 formats
        meta = gist.get(gist_key, {})
        if not meta:
            short = set_number.split("/")[0].upper()
            meta = gist.get(short, {})

    name = row.get("name") or meta.get("name", "Unknown")
    card_type = meta.get("card_type", "Unknown")
    image_url = meta.get("image_thumb") or meta.get("image_url", "")

    # Token cards use collector numbers like UNL-T01 (not every UNL string with "T")
    if set_code == "UNL" and re.search(r"\bUNL-T\d+", (raw_text + " " + url).upper()):
        if card_type == "Unknown":
            card_type = "Token"

    card_id = make_card_id(set_code, num, foil_status, url)

    return {
        "id": card_id,
        "name": name,
        "set_code": set_code,
        "set_number": set_number,
        "card_type": card_type,
        "foil_status": foil_status,
        "price": row.get("price", ""),
        "url": url,
        "image_url": image_url,
        "collected_at": row.get("collected_at", ""),
    }


def main() -> int:
    if not CSV_PATH.exists():
        print(f"CSV not found: {CSV_PATH}", file=sys.stderr)
        return 1

    riftscribe = fetch_riftscribe_catalog()
    gist = fetch_gist_catalog() if len(riftscribe) < 100 else {}

    cards: list[dict] = []
    seen_ids: set[str] = set()

    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            card = enrich_row(row, riftscribe, gist)
            # Deduplicate by id
            base_id = card["id"]
            cid = base_id
            n = 1
            while cid in seen_ids:
                cid = f"{base_id}-{n}"
                n += 1
            card["id"] = cid
            seen_ids.add(cid)
            cards.append(card)

    if len(cards) < 100:
        print(
            f"Refusing to overwrite {OUT_PATH}: only {len(cards)} cards from CSV "
            "(expected ≥100). Keeping previous cards.json if present.",
            file=sys.stderr,
        )
        return 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds"),
        "count": len(cards),
        "cards": cards,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(cards)} cards → {OUT_PATH}")

    matched = sum(1 for c in cards if c["card_type"] != "Unknown")
    print(f"Matched card types: {matched}/{len(cards)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
