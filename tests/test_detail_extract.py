"""Unit tests for detail-page market price / name parsing."""

from __future__ import annotations

import unittest

from scrape import (
    ApiResponseRecord,
    api_group_to_url,
    api_lookup_key,
    classify_detail_failure,
    extract_market_price,
    group_api_cards,
    is_app_check_auth_failure,
    is_card_not_found,
    normalize_short_card_id,
    parse_detail_market_prices,
    parse_name_from_document_title,
    row_from_api_group,
    rows_from_api_cards,
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


class AppCheckAuthTests(unittest.TestCase):
    def test_is_app_check_auth_failure(self):
        body = '{"error":"App Check token required"}'
        self.assertTrue(is_app_check_auth_failure(401, body))
        self.assertFalse(is_app_check_auth_failure(403, body))
        self.assertFalse(is_app_check_auth_failure(401, "unauthorized"))

    def test_classify_prefers_api_auth_over_card_not_found(self):
        body = "Bilgewater Market Card not found."
        records = [
            ApiResponseRecord(
                "https://api.bilgewatermarket.com/api/cards/detail?card_id=UNL-015",
                401,
                '{"message":"App Check token required"}',
            )
        ]
        self.assertEqual(
            classify_detail_failure(
                title="",
                body=body,
                blocked=False,
                api_records=records,
            ),
            "skipped_api_auth",
        )

    def test_classify_real_card_not_found(self):
        body = "Bilgewater Market Card not found."
        records = [
            ApiResponseRecord(
                "https://api.bilgewatermarket.com/api/cards/detail?card_id=UNL-999",
                404,
                '{"error":"not found"}',
            )
        ]
        self.assertEqual(
            classify_detail_failure(
                title="",
                body=body,
                blocked=False,
                api_records=records,
            ),
            "card_not_found",
        )

    def test_classify_detail_empty_without_signals(self):
        self.assertEqual(
            classify_detail_failure(
                title="",
                body="loading",
                blocked=False,
                api_records=[],
            ),
            "detail_empty",
        )


class ApiCardRowTests(unittest.TestCase):
    def test_normalize_short_card_id_suffix_case(self):
        self.assertEqual(normalize_short_card_id("UNL-028A"), "UNL-028a")
        self.assertEqual(normalize_short_card_id("UNL-015"), "UNL-015")

    def test_api_lookup_key_from_url(self):
        self.assertEqual(
            api_lookup_key("https://bilgewatermarket.com/cards/UNL-028A?print_variation=showcase"),
            ("UNL-028a", "showcase"),
        )

    def test_group_api_cards_merges_cn_en(self):
        cards = [
            {
                "card_id": "UNL-015/219",
                "name": "Right of Conquest",
                "print_variation": "normal",
                "language": "english",
                "price_usd": 0.08,
                "price_cny": None,
            },
            {
                "card_id": "UNL-015/219",
                "name": "占山为王",
                "print_variation": "normal",
                "language": "simplified_chinese",
                "price_usd": None,
                "price_cny": 0.5,
            },
        ]
        groups = group_api_cards(cards)
        row = row_from_api_group(
            groups[("UNL-015", "normal")],
            base_url="https://bilgewatermarket.com",
            source_url="https://bilgewatermarket.com/cards",
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["name"], "Right of Conquest")
        self.assertEqual(row["en_price"], "$0.08")
        self.assertEqual(row["cn_price"], "¥0.50")
        self.assertEqual(row["url"], "https://bilgewatermarket.com/cards/UNL-015")

    def test_rows_from_api_cards_respects_limit(self):
        cards = [
            {
                "card_id": "UNL-001/219",
                "name": "A",
                "print_variation": "normal",
                "language": "english",
                "price_usd": 1.0,
            },
            {
                "card_id": "UNL-001/219",
                "name": "A",
                "print_variation": "normal",
                "language": "simplified_chinese",
                "price_cny": 2.0,
            },
            {
                "card_id": "UNL-002/219",
                "name": "B",
                "print_variation": "normal",
                "language": "english",
                "price_usd": 3.0,
            },
            {
                "card_id": "UNL-002/219",
                "name": "B",
                "print_variation": "normal",
                "language": "simplified_chinese",
                "price_cny": 4.0,
            },
        ]
        rows = rows_from_api_cards(
            cards,
            base_url="https://bilgewatermarket.com",
            source_url="https://bilgewatermarket.com/cards",
            limit=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            api_group_to_url("https://bilgewatermarket.com", "UNL-001", "foiled"),
            "https://bilgewatermarket.com/cards/UNL-001?print_variation=foiled",
        )


if __name__ == "__main__":
    unittest.main()
