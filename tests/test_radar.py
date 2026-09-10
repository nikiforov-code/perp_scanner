import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from user_settings import UserSettings, sanitize
from radar import select_hits, build_alert, radar_users, COOLDOWN_SEC

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 9, 11, 15, 0, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())
NOW_MS = NOW_TS * 1000
HOUR_MS = 3600 * 1000


def coin(symbol="STORJUSDT", exchange="BYBIT", rate=-1.87, vol=30_000_000.0, in_hours=5.0):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "funding_rate": rate,
        "next_funding_ms": NOW_MS + int(in_hours * HOUR_MS),
        "url": "https://www.bybit.com/trade/usdt/STORJUSDT",
        "mark_px": 0.0286,
        "vol_usdt_24h": vol,
    }


def user(**kw):
    return sanitize(UserSettings(**kw))


class TestSelectHits(unittest.TestCase):
    def setUp(self):
        self.s = user()  # радар: -1.5% и 5M по умолчанию

    def test_fat_negative_funding_is_caught(self):
        self.assertEqual([h["symbol"] for h in select_hits([coin()], self.s, NOW_TS, {})], ["STORJUSDT"])

    def test_rate_above_threshold_is_ignored(self):
        self.assertEqual(select_hits([coin(rate=-1.2)], self.s, NOW_TS, {}), [])

    def test_exactly_at_threshold_is_caught(self):
        self.assertEqual(len(select_hits([coin(rate=-1.5)], self.s, NOW_TS, {})), 1)

    def test_thin_volume_is_ignored(self):
        self.assertEqual(select_hits([coin(vol=4_000_000)], self.s, NOW_TS, {}), [])

    def test_positive_funding_is_ignored(self):
        """Торгуется только отрицательная сторона."""
        self.assertEqual(select_hits([coin(rate=+3.0)], self.s, NOW_TS, {}), [])

    def test_other_exchanges_are_ignored(self):
        for ex in ("BINANCE", "OKX", "GATE"):
            self.assertEqual(select_hits([coin(exchange=ex)], self.s, NOW_TS, {}), [], ex)

    def test_dirty_symbol_is_ignored(self):
        self.assertEqual(select_hits([coin(symbol="比特币USDT")], self.s, NOW_TS, {}), [])

    def test_missing_funding_time_is_ignored(self):
        c = coin()
        c["next_funding_ms"] = 0
        self.assertEqual(select_hits([c], self.s, NOW_TS, {}), [])

    def test_horizon_does_not_apply(self):
        """Радар существует ровно для того, чтобы показывать то, что горизонт прячет."""
        self.assertEqual(len(select_hits([coin(in_hours=7)], self.s, NOW_TS, {})), 1)

    def test_deepest_first(self):
        coins = [coin(symbol="AUSDT", rate=-1.6), coin(symbol="BUSDT", rate=-4.2), coin(symbol="CUSDT", rate=-2.0)]
        got = [h["symbol"] for h in select_hits(coins, self.s, NOW_TS, {})]
        self.assertEqual(got, ["BUSDT", "CUSDT", "AUSDT"])

    def test_personal_thresholds(self):
        strict = user(radar_rate=-3.0, radar_vol=50_000_000)
        loose = user(radar_rate=-1.0, radar_vol=1_000_000)
        c = [coin(rate=-2.0, vol=10_000_000)]
        self.assertEqual(select_hits(c, strict, NOW_TS, {}), [])
        self.assertEqual(len(select_hits(c, loose, NOW_TS, {})), 1)

    def test_positive_radar_rate_setting_still_works(self):
        """Пользователь ввёл 1.5 без минуса — санитайз чинит знак."""
        self.assertEqual(len(select_hits([coin()], user(radar_rate=1.5), NOW_TS, {})), 1)


class TestCooldown(unittest.TestCase):
    def setUp(self):
        self.s = user()

    def test_silent_right_after_alert(self):
        last = {("u", "STORJUSDT"): NOW_TS}
        self.assertEqual(select_hits([coin()], self.s, NOW_TS, {"STORJUSDT": NOW_TS}), [])

    def test_still_silent_after_eleven_hours(self):
        sent = NOW_TS - 11 * 3600
        self.assertEqual(select_hits([coin()], self.s, NOW_TS, {"STORJUSDT": sent}), [])

    def test_fires_again_after_cooldown(self):
        sent = NOW_TS - COOLDOWN_SEC - 60
        self.assertEqual(len(select_hits([coin()], self.s, NOW_TS, {"STORJUSDT": sent})), 1)

    def test_jitter_around_threshold_does_not_repeat(self):
        """Монета ушла выше порога и через час вернулась — второго сообщения нет."""
        sent_hours = {"STORJUSDT": NOW_TS - 3600}
        self.assertEqual(select_hits([coin(rate=-1.2)], self.s, NOW_TS, sent_hours), [])
        self.assertEqual(select_hits([coin(rate=-1.9)], self.s, NOW_TS, sent_hours), [])

    def test_cooldown_is_twelve_hours(self):
        self.assertEqual(COOLDOWN_SEC, 12 * 3600)


class TestBuildAlert(unittest.TestCase):
    def test_message_carries_the_essentials(self):
        text = build_alert(coin(), user(), TZ, NOW_MS)
        self.assertIn("STORJ", text)
        self.assertIn("-1.87%", text)
        self.assertIn("Bybit", text)
        self.assertIn("https://www.bybit.com/trade/usdt/STORJUSDT", text)

    def test_shows_volume_in_millions(self):
        self.assertIn("30,0M", build_alert(coin(), user(), TZ, NOW_MS))

    def test_shows_time_until_funding(self):
        text = build_alert(coin(in_hours=5), user(), TZ, NOW_MS)
        self.assertIn("5 ч", text)

    def test_fits_telegram_limit(self):
        self.assertLess(len(build_alert(coin(), user(), TZ, NOW_MS)), 4096)


class TestRadarUsers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.items = [coin()]
        self.sent = []
        self.marks = {}

    async def send_ok(self, uid, text):
        self.sent.append(uid)

    def mark(self, uid, symbol, ts):
        self.marks[(uid, symbol)] = ts

    async def run_radar(self, users, send=None, marks=None, **kw):
        return await radar_users(
            users, self.items, NOW_TS,
            send or self.send_ok,
            marks if marks is not None else self.marks,
            self.mark,
            **kw,
        )

    async def test_sends_and_marks(self):
        stats = await self.run_radar({1: user()})
        self.assertEqual(stats["sent"], [1])
        self.assertEqual(self.marks[(1, "STORJUSDT")], NOW_TS)

    async def test_notifications_off_is_skipped(self):
        stats = await self.run_radar({1: user(notify_enabled=False)})
        self.assertEqual(stats["sent"], [])

    async def test_one_failure_does_not_block_others(self):
        async def send(uid, text):
            if uid == 2:
                raise RuntimeError("boom")
            self.sent.append(uid)

        with self.assertLogs("funding-bot.radar", level="ERROR"):
            stats = await self.run_radar({1: user(), 2: user(), 3: user()}, send=send)
        self.assertEqual(sorted(self.sent), [1, 3])
        self.assertEqual(stats["failed"], [2])

    async def test_failed_send_is_not_marked(self):
        async def send(uid, text):
            raise RuntimeError("boom")

        with self.assertLogs("funding-bot.radar", level="ERROR"):
            await self.run_radar({1: user()}, send=send)
        self.assertEqual(self.marks, {})

    async def test_blocked_user_reported(self):
        class Blocked(Exception):
            pass

        async def send(uid, text):
            raise Blocked()

        off = []
        stats = await self.run_radar(
            {1: user()}, send=send,
            is_blocked=lambda e: isinstance(e, Blocked),
            on_blocked=lambda uid, s: off.append(uid),
        )
        self.assertEqual(stats["blocked"], [1])
        self.assertEqual(off, [1])

    async def test_marks_are_per_user(self):
        marks = {(1, "STORJUSDT"): NOW_TS}
        stats = await self.run_radar({1: user(), 2: user()}, marks=marks)
        self.assertEqual(stats["sent"], [2])


if __name__ == "__main__":
    unittest.main()
