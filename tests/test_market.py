import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market import Market


def rec(exchange, funding=0.1, next_ms=1789041600000, mark=100.0, vol=5_000_000.0):
    return {
        "exchange": exchange,
        "funding_pct": funding,
        "next_ms": next_ms,
        "mark_px": mark,
        "link": "https://x",
        "vol_usdt_24h": vol,
    }


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.m = Market()
        self.m.merge("BINANCE", {"BTCUSDT": rec("BINANCE")})

    def test_successful_merge_marks_fresh(self):
        self.assertFalse(self.m.status["BINANCE"]["stale"])
        self.assertTrue(self.m.status["BINANCE"]["ok"])

    def test_error_keeps_previous_data(self):
        """Сбой биржи не должен опустошать её данные — иначе дайджест теряет монеты."""
        self.m.merge("BINANCE", {}, err="ConnectTimeout")
        self.assertIn("BTCUSDT", self.m.per_exchange["BINANCE"])
        self.assertTrue(self.m.status["BINANCE"]["stale"])
        self.assertEqual(self.m.status["BINANCE"]["err"], "ConnectTimeout")

    def test_empty_response_does_not_wipe(self):
        self.m.merge("BINANCE", {})
        self.assertIn("BTCUSDT", self.m.per_exchange["BINANCE"])
        self.assertTrue(self.m.status["BINANCE"]["stale"])

    def test_recovery_replaces_data(self):
        self.m.merge("BINANCE", {}, err="boom")
        self.m.merge("BINANCE", {"ETHUSDT": rec("BINANCE")})
        self.assertEqual(list(self.m.per_exchange["BINANCE"]), ["ETHUSDT"])
        self.assertFalse(self.m.status["BINANCE"]["stale"])

    def test_error_on_never_seen_exchange_is_safe(self):
        self.m.merge("GATE", {}, err="boom")
        self.assertEqual(self.m.per_exchange["GATE"], {})


class TestItems(unittest.TestCase):
    def setUp(self):
        self.m = Market()
        self.m.merge("BINANCE", {"BTCUSDT": rec("BINANCE", funding=-0.5)})
        self.m.merge("BYBIT", {"BTCUSDT": rec("BYBIT", funding=0.3, mark=101.0)})

    def test_items_have_bot_field_names(self):
        item = next(i for i in self.m.items() if i["exchange"] == "BINANCE")
        self.assertEqual(item["symbol"], "BTCUSDT")
        self.assertEqual(item["funding_rate"], -0.5)
        self.assertEqual(item["next_funding_ms"], 1789041600000)
        self.assertEqual(item["url"], "https://x")
        self.assertIn("vol_usdt_24h", item)
        self.assertIn("mark_px", item)

    def test_items_cover_all_exchanges(self):
        self.assertEqual(len(self.m.items()), 2)

    def test_items_skip_records_without_next_funding(self):
        self.m.merge("GATE", {"BTCUSDT": rec("GATE", next_ms=0)})
        self.assertEqual(len([i for i in self.m.items() if i["exchange"] == "GATE"]), 0)


class TestPriceSpread(unittest.TestCase):
    def setUp(self):
        self.m = Market()
        self.m.merge("BINANCE", {"BTCUSDT": rec("BINANCE", mark=100.0)})
        self.m.merge("BYBIT", {"BTCUSDT": rec("BYBIT", mark=101.0)})

    def test_spread_between_binance_and_bybit(self):
        got = self.m.price_spread("btc")
        self.assertEqual(got["symbol"], "BTCUSDT")
        self.assertAlmostEqual(got["spread_pct"], (100.0 / 101.0 - 1) * 100)

    def test_accepts_full_symbol(self):
        self.assertIsNotNone(self.m.price_spread("BTCUSDT"))

    def test_unknown_symbol_is_none(self):
        self.assertIsNone(self.m.price_spread("NOPE"))

    def test_zero_price_is_none_not_crash(self):
        """Раньше нулевая цена Bybit роняла эндпоинт делением на ноль."""
        self.m.merge("BYBIT", {"BTCUSDT": rec("BYBIT", mark=0.0)})
        self.assertIsNone(self.m.price_spread("BTC"))

    def test_empty_symbol_is_none(self):
        self.assertIsNone(self.m.price_spread(""))


class TestMinVolume(unittest.TestCase):
    def test_floor_protects_from_zero_threshold(self):
        from market import okx_min_volume
        self.assertEqual(okx_min_volume([0.0, 5_000_000.0]), 500_000)

    def test_uses_softest_user_threshold(self):
        from market import okx_min_volume
        self.assertEqual(okx_min_volume([10_000_000.0, 1_000_000.0]), 1_000_000)

    def test_no_users_falls_back_to_floor(self):
        from market import okx_min_volume
        self.assertEqual(okx_min_volume([]), 500_000)


if __name__ == "__main__":
    unittest.main()
