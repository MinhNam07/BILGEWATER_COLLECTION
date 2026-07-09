"""HTTP client for Bilgewater scraper API (Bearer auth, bypasses App Check)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urljoin

if TYPE_CHECKING:
    from scrape import BrowserSession

log = logging.getLogger("bilgewater")

DEFAULT_API_BASE = "https://api.bilgewatermarket.com"
REQUEST_TIMEOUT_SEC = 60


class ScraperAPIError(Exception):
    """Scraper API request failed."""


class ScraperAPIClient:
    def __init__(
        self,
        api_base: str,
        token: str,
        *,
        user_agent: str = "BilgewaterDailyCollector/1.0",
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token.strip()
        self.user_agent = user_agent

    @classmethod
    def from_env(cls, *, user_agent: str = "BilgewaterDailyCollector/1.0") -> ScraperAPIClient | None:
        token = (os.getenv("SCRAPER_API_TOKEN") or "").strip()
        if not token:
            return None
        api_base = (os.getenv("SCRAPER_API_BASE") or DEFAULT_API_BASE).strip()
        return cls(api_base, token, user_agent=user_agent)

    def _request(self, path: str, *, params: dict[str, str] | None = None) -> dict | list:
        url = urljoin(self.api_base + "/", path.lstrip("/"))
        if params:
            url += "?" + urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise ScraperAPIError(f"HTTP {exc.code} for {url}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ScraperAPIError(f"request failed for {url}: {exc}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScraperAPIError(f"invalid JSON from {url}") from exc

    def fetch_cards_with_prices(self) -> list[dict] | None:
        data = self._request("/api/scraper/cards-with-prices")
        if isinstance(data, dict):
            cards = data.get("cards")
            if isinstance(cards, list) and cards:
                log.info("scraper API cards-with-prices: %d entries", len(cards))
                return cards
        return None

    def fetch_card_detail(self, card_id: str, print_variation: str = "normal") -> list[dict] | None:
        data = self._request(
            "/api/scraper/cards/detail",
            params={"card_id": card_id, "print_variation": print_variation},
        )
        if isinstance(data, dict):
            card = data.get("card")
            if isinstance(card, dict):
                return [card]
            cards = data.get("cards")
            if isinstance(cards, list) and cards:
                return cards
        if isinstance(data, list) and data:
            return data
        return None

    def populate_session(self, session: BrowserSession) -> bool:
        cards = self.fetch_cards_with_prices()
        if cards:
            session.set_api_cards(cards)
            return True
        return False

    def fetch_detail_row_for_url(
        self,
        url: str,
        *,
        base_url: str,
        source_url: str | None = None,
    ) -> dict | None:
        from scrape import (
            api_lookup_key,
            group_api_cards,
            row_from_api_group,
        )

        key = api_lookup_key(url)
        if not key:
            return None
        short_id, pv = key
        cards = self.fetch_card_detail(short_id, pv)
        if not cards:
            return None
        groups = group_api_cards(cards)
        group = groups.get(key)
        if not group:
            return None
        row = row_from_api_group(
            group,
            base_url=base_url,
            source_url=source_url or url,
            href=url,
        )
        if row:
            row["source_url"] = source_url or url
        return row
