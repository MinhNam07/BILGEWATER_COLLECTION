"""Unit tests for detail-page market price / name parsing."""

from __future__ import annotations

import unittest

from scrape import (
    extract_market_price,
    is_card_not_found,
    parse_detail_market_prices,
    parse_name_from_document_title,
)


class DetailMarketPriceTests(unittest.TestCase):
    def test_extract_cn_cny_code_style(self):
        text = "MARKET PRICES CN CNY 7.11 0.0% EN USD 1.61 0.0%"
        self.assertEqual(extract_market_price(text, "CN"), "7.11")

    def test_extract_en_usd_code_style(self):
        text = "MARKET PRICES CN CNY 7.11 0.0% EN USD 1.61 0.0%"
        self.assertEqual(extract_market_price(text, "EN"), "1.61")

    def test_extract_with_newlines_and_spacing(self):
        text = "MARKET PRICES\nCN\nCNY\n7.11\n0.0%\nEN\nUSD\n1.61\n0.0%"
        self.assertEqual(extract_market_price(text, "CN"), "7.11")
        self.assertEqual(extract_market_price(text, "EN"), "1.61")

    def test_extract_still_supports_symbol_style(self):
        text = "Arena Kingpin CN ¥0.53 EN $0.04"
        self.assertEqual(extract_market_price(text, "CN"), "0.53")
        self.assertEqual(extract_market_price(text, "EN"), "0.04")

    def test_parse_detail_prefers_market_prices_over_other_prints(self):
        text = (
            "OTHER PRINTS PRINTS CN EN UNL-001 Foiled ¥0.50 $0.12 "
            "Arena Kingpin UNL-001 NORMAL COMMON "
            "MARKET PRICES CN CNY 0.53 0.0% EN USD 0.04 0.0% "
            "CN / EN PRICE COMPARISON"
        )
        cn, en = parse_detail_market_prices(text)
        self.assertEqual(cn, "0.53")
        self.assertEqual(en, "0.04")

    def test_parse_name_from_document_title(self):
        title = "Katarina, Reckless Price | Riftbound Card Price History"
        self.assertEqual(parse_name_from_document_title(title), "Katarina, Reckless Price")

    def test_is_card_not_found(self):
        self.assertTrue(is_card_not_found("Bilgewater Market Home Card not found."))
        self.assertFalse(is_card_not_found("MARKET PRICES CN CNY 0.53 EN USD 0.04"))


if __name__ == "__main__":
    unittest.main()
