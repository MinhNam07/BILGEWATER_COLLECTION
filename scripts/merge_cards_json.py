#!/usr/bin/env python3
"""Merge a targeted cards patch into web/cards.json without deleting entries."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge cards_patch.json into cards.json")
    ap.add_argument("--current", required=True, help="Existing web/cards.json path")
    ap.add_argument("--patch", required=True, help="Targeted patch JSON path")
    ap.add_argument("--out", required=True, help="Output JSON path (usually web/cards.json)")
    args = ap.parse_args()

    current_path = Path(args.current)
    patch_path = Path(args.patch)
    out_path = Path(args.out)

    cur = load_json(current_path)
    patch = load_json(patch_path)

    cur_cards = list(cur.get("cards") or [])
    patch_cards = list(patch.get("cards") or [])

    if not cur_cards:
        print(f"::error::Current catalog empty or missing: {current_path}", file=sys.stderr)
        return 1
    if not patch_cards:
        print(f"::error::Patch catalog empty or missing: {patch_path}", file=sys.stderr)
        return 1

    by_id: dict[str, dict] = {}
    order: list[str] = []
    for c in cur_cards:
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        if cid not in by_id:
            order.append(cid)
        by_id[cid] = c

    updated = 0
    added = 0
    for p in patch_cards:
        cid = str(p.get("id") or "").strip()
        if not cid:
            continue
        if cid in by_id:
            by_id[cid] = p
            updated += 1
        else:
            by_id[cid] = p
            order.append(cid)
            added += 1

    merged = [by_id[cid] for cid in order if cid in by_id]
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(merged),
        "cards": merged,
    }

    if len(merged) < len(cur_cards):
        print(
            f"::error::Refusing to write {out_path}: merged count {len(merged)} < current {len(cur_cards)}",
            file=sys.stderr,
        )
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Merged patch: updated={updated} added={added} total={len(merged)} → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

