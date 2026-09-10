import os
import sys
import unittest
from datetime import timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formatting import (
    fmt_coin,
    fmt_rate,
    is_clean_symbol,
    calc_coin_width,
    build_binance_price_map,
    format_delta_vs_binance,
    format_item_line,
    split_html_chunks,
)

MSK = timezone(timedelta(hours=3))


class TestSmallFormatters(unittest.TestCase):
    def test_fmt_coin_strips_usdt(self):
        self.assertEqual(fmt_coin("BTCUSDT"), "BTC")
        self.assertEqual(fmt_coin(""), "")

    def test_fmt_rate_keeps_minus_drops_plus(self):
        self.assertEqual(fmt_rate(-1.47), "-1.47%")
        self.assertEqual(fmt_rate(0.45), "0.45%")

    def test_is_clean_symbol_rejects_non_latin(self):
        self.assertTrue(is_clean_symbol("BTCUSDT"))
        self.assertTrue(is_clean_symbol("1000PEPEUSDT"))
        self.assertFalse(is_clean_symbol("比特币USDT"))
        self.assertFalse(is_clean_symbol(""))

    def test_calc_coin_width_follows_longest_coin(self):
        items = [{"symbol": "BTCUSDT"}, {"symbol": "1000PEPEUSDT"}]
        self.assertEqual(calc_coin_width(items), len("1000PEPE"))

    def test_calc_coin_width_respects_bounds(self):
        self.assertEqual(calc_coin_width([{"symbol": "AUSDT"}], min_w=4), 4)
        long_one = [{"symbol": "A" * 40 + "USDT"}]
        self.assertEqual(calc_coin_width(long_one, max_w=10), 10)


class TestBinanceDelta(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"exchange": "BINANCE", "symbol": "BTCUSDT", "mark_px": 100.0},
            {"exchange": "BYBIT", "symbol": "BTCUSDT", "mark_px": 101.0},
            {"exchange": "BINANCE", "symbol": "BADUSDT", "mark_px": 0.0},
        ]

    def test_price_map_skips_non_positive(self):
        m = build_binance_price_map(self.items)
        self.assertEqual(m, {"BTCUSDT": 100.0})

    def test_delta_none_for_binance_itself(self):
        m = build_binance_price_map(self.items)
        self.assertIsNone(format_delta_vs_binance("BINANCE", "BTCUSDT", 100.0, m))

    def test_delta_uses_comma_decimal(self):
        m = build_binance_price_map(self.items)
        self.assertEqual(format_delta_vs_binance("BYBIT", "BTCUSDT", 101.0, m), "-1,0%")

    def test_delta_none_when_binance_price_unknown(self):
        self.assertIsNone(format_delta_vs_binance("BYBIT", "ETHUSDT", 5.0, {}))


class TestItemLine(unittest.TestCase):
    def line(self, exchange, url="https://x"):
        item = {
            "symbol": "BTCUSDT",
            "exchange": exchange,
            "url": url,
            "next_funding_ms": 1789041600000,
            "funding_rate": -0.47,
            "vol_usdt_24h": 12_300_000.0,
            "mark_px": 101.0,
        }
        return format_item_line(item, coin_w=4, tz=MSK, binance_prices={"BTCUSDT": 100.0})

    def test_no_trailing_space_inside_code(self):
        """Мёртвый delta_block оставлял висячий пробел перед </code>."""
        self.assertNotIn(" </code>", self.line("BINANCE"))

    def test_binance_line_has_short_label(self):
        self.assertIn(">BIN</a>", self.line("BINANCE"))

    def test_other_exchange_shows_delta(self):
        self.assertIn("Δ-1,0%", self.line("BYBIT"))

    def test_delta_label_without_url(self):
        self.assertIn("Δ", self.line("OKX", url=""))

    def test_negative_rate_is_green(self):
        self.assertTrue(self.line("BINANCE").startswith("🟢"))


class TestSplitHtmlChunks(unittest.TestCase):
    def test_short_text_stays_one_chunk(self):
        self.assertEqual(split_html_chunks("раз\nдва", 100), ["раз\nдва"])

    def test_splits_on_line_boundaries(self):
        text = "\n".join(f"строка {i}" for i in range(20))
        chunks = split_html_chunks(text, 40)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 40)

    def test_never_breaks_a_tag(self):
        text = "\n".join(f"<code>строка {i}</code>" for i in range(20))
        for c in split_html_chunks(text, 60):
            self.assertEqual(c.count("<code>"), c.count("</code>"))

    def test_oversized_single_line_is_kept_not_dropped(self):
        """Строка длиннее лимита должна уехать отдельным куском, а не потеряться."""
        long_line = "x" * 50
        chunks = split_html_chunks(f"мало\n{long_line}\nмало", 20)
        self.assertIn(long_line, "\n".join(chunks))

    def test_no_content_is_lost(self):
        text = "\n".join(f"строка {i}" for i in range(50))
        joined = "\n".join(split_html_chunks(text, 45))
        self.assertEqual(joined.split(), text.split())

    def test_empty_text_gives_no_chunks(self):
        self.assertEqual(split_html_chunks("   ", 100), [])


if __name__ == "__main__":
    unittest.main()
