#!/usr/bin/env python3
"""Generate web/config.js from environment variables (Vercel build step)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "config.js"


def main() -> None:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_ANON_KEY", "")

    content = f"""// Auto-generated at build time — do not commit secrets.
window.APP_CONFIG = {{
  supabaseUrl: {json_escape(url)},
  supabaseAnonKey: {json_escape(key)},
}};
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUT} (supabase configured: {bool(url and key)})")


def json_escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


if __name__ == "__main__":
    main()
