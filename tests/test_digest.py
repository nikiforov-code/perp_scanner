import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from user_settings import UserSettings, sanitize
from digest import (
    select_items,
    build_message,
    due_hour_key,
    hour_key,
    passes_threshold,
    MAX_PER_EXCHANGE,
    GRACE_MINUTES,
)

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 9, 10, 15, 0, tzinfo=TZ)
NOW_MS = int(NOW.timestamp() * 1000)
HOUR = 60 * 60 * 1000


def item(ex="BINANCE", sym="BTCUSDT", rate=-0.5, in_hours=1.0, vol=20_000_000.0):
    return {
        "symbol": sym,
        "exchange": ex,
        "funding_rate": rate,
        "next_funding_ms": NOW_MS + int(in_hours * HOUR),
        "url": "https://x",
        "mark_px": 100.0,
        "vol_usdt_24h": vol,
    }


class TestThreshold(unittest.TestCase):
    def test_passes_both_directions(self):
        self.assertTrue(passes_threshold(0.5, 0.45, -0.45))
        self.assertTrue(passes_threshold(-0.5, 0.45, -0.45))
        self.assertFalse(passes_threshold(0.1, 0.45, -0.45))


class TestSelectItems(unittest.TestCase):
    def setUp(self):
        self.s = sanitize(UserSettings())

    def test_keeps_matching_item(self):
        groups = select_items([item()], self.s, NOW_MS)
        self.assertEqual(len(groups["BINANCE"]), 1)

    def test_drops_below_threshold(self):
        groups = select_items([item(rate=-0.1)], self.s, NOW_MS)
        self.assertEqual(groups["BINANCE"], [])

    def test_drops_below_volume(self):
        groups = select_items([item(vol=1_000_000.0)], self.s, NOW_MS)
        self.assertEqual(groups["BINANCE"], [])

    def test_drops_beyond_horizon(self):
        groups = select_items([item(in_hours=9)], self.s, NOW_MS)
        self.assertEqual(groups["BINANCE"], [])

    def test_drops_already_passed(self):
        groups = select_items([item(in_hours=-1)], self.s, NOW_MS)
        self.assertEqual(groups["BINANCE"], [])

    def test_drops_foreign_exchange(self):
        groups = select_items([item(ex="MEXC")], self.s, NOW_MS)
        self.assertNotIn("MEXC", groups)

    def test_drops_dirty_symbol(self):
        groups = select_items([item(sym="比特币USDT")], self.s, NOW_MS)
        self.assertEqual(groups["BINANCE"], [])

    def test_sorted_by_time_then_rate(self):
        items = [
            item(sym="AUSDT", in_hours=2, rate=-0.9),
            item(sym="BUSDT", in_hours=1, rate=-0.5),
            item(sym="CUSDT", in_hours=1, rate=-0.8),
        ]
        got = [x["symbol"] for x in select_items(items, self.s, NOW_MS)["BINANCE"]]
        self.assertEqual(got, ["CUSDT", "BUSDT", "AUSDT"])

    def test_broken_thresholds_no_longer_match_everything(self):
        """Пороги +1/+1 ловили весь рынок; после sanitize остаётся нормальный фильтр."""
        s = sanitize(UserSettings(pos_threshold=1.0, neg_threshold=1.0, vol_threshold_usdt=0))
        groups = select_items([item(rate=0.01)], s, NOW_MS)
        self.assertEqual(groups["BINANCE"], [])


class TestBuildMessage(unittest.TestCase):
    def setUp(self):
        self.s = sanitize(UserSettings())

    def test_caps_rows_per_exchange(self):
        items = [item(sym=f"C{i}USDT") for i in range(50)]
        groups = select_items(items, self.s, NOW_MS)
        text = build_message(groups, self.s, "заголовок", NOW, TZ)
        self.assertEqual(text.count("🟢"), MAX_PER_EXCHANGE)

    def test_shows_all_four_exchanges(self):
        text = build_message(select_items([item()], self.s, NOW_MS), self.s, "з", NOW, TZ)
        for name in ("Binance", "Bybit", "OKX", "Gate"):
            self.assertIn(name, text)

    def test_reports_hidden_rows(self):
        items = [item(sym=f"C{i}USDT") for i in range(50)]
        text = build_message(select_items(items, self.s, NOW_MS), self.s, "з", NOW, TZ)
        self.assertIn("50", text)

    def test_header_carries_filters(self):
        text = build_message(select_items([item()], self.s, NOW_MS), self.s, "з", NOW, TZ)
        self.assertIn("0.45", text)
        self.assertIn("Горизонт 4 часа", text)


class TestDueHourKey(unittest.TestCase):
    """Раньше дайджест требовал точного попадания в минуту: подвис цикл — час потерян."""

    def setUp(self):
        self.s = sanitize(UserSettings(digest_before_hour_minutes=20))  # цель :40

    def at(self, hh, mm):
        return datetime(2026, 9, 10, hh, mm, tzinfo=TZ)

    def test_fires_at_target_minute(self):
        self.assertEqual(due_hour_key(self.s, self.at(15, 40)), hour_key(self.at(15, 0)))

    def test_silent_before_target(self):
        self.assertIsNone(due_hour_key(self.s, self.at(15, 39)))

    def test_fires_after_missed_minute(self):
        self.assertEqual(due_hour_key(self.s, self.at(15, 45)), hour_key(self.at(15, 0)))

    def test_gives_up_after_grace_window(self):
        late = self.at(15, 40 + GRACE_MINUTES + 1)
        self.assertIsNone(due_hour_key(self.s, late))

    def test_same_key_across_window(self):
        keys = {due_hour_key(self.s, self.at(15, m)) for m in range(40, 40 + GRACE_MINUTES)}
        self.assertEqual(len(keys), 1)

    def test_next_hour_gives_new_key(self):
        self.assertNotEqual(
            due_hour_key(self.s, self.at(15, 40)),
            due_hour_key(self.s, self.at(16, 40)),
        )

    def test_target_minute_zero(self):
        s = sanitize(UserSettings(digest_before_hour_minutes=59))  # цель :01
        self.assertEqual(due_hour_key(s, self.at(15, 1)), hour_key(self.at(15, 0)))
        self.assertIsNone(due_hour_key(s, self.at(15, 0)))


if __name__ == "__main__":
    unittest.main()
