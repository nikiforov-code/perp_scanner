import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from user_settings import UserSettings, sanitize
from notifier import notify_users

HOUR = 60 * 60 * 1000

# Фиксированное «сейчас»: 15:40 по UTC+3. При digest_before=20 целевая минута — :40,
# то есть дайджест положено слать ровно сейчас.
TZ = timezone(timedelta(hours=3))
NOW_LOCAL = datetime(2026, 9, 10, 15, 40, tzinfo=TZ)
NOW_MS = int(NOW_LOCAL.timestamp() * 1000)


def clock(tz):
    return NOW_LOCAL.astimezone(tz)


class Blocked(Exception):
    """Стенд-ин для aiogram TelegramForbiddenError."""


def due_settings(**kw):
    """Настройки, при которых дайджест положено слать в NOW_LOCAL."""
    kw.setdefault("digest_before_hour_minutes", 20)  # цель :40
    kw.setdefault("utc_offset_hours", 3)
    return sanitize(UserSettings(**kw))


def market_items(n=3):
    return [
        {
            "symbol": f"C{i}USDT",
            "exchange": "BINANCE",
            "funding_rate": -0.9,
            "next_funding_ms": NOW_MS + HOUR,
            "url": "https://x",
            "mark_px": 100.0,
            "vol_usdt_24h": 50_000_000.0,
        }
        for i in range(n)
    ]


class TestNotifyUsers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.items = market_items()
        self.sent = []

    async def send_ok(self, uid, text):
        self.sent.append(uid)

    async def run_notify(self, users, send=None, sent_hours=None, **kw):
        return await notify_users(
            users,
            self.items,
            NOW_MS,
            send or self.send_ok,
            sent_hours if sent_hours is not None else {},
            now_provider=clock,
            **kw,
        )

    async def test_sends_to_due_user(self):
        stats = await self.run_notify({1: due_settings()})
        self.assertEqual(stats["sent"], [1])

    async def test_one_failure_does_not_block_others(self):
        """Главный баг аудита: падение на одном пользователе обрывало очередь."""
        async def send(uid, text):
            if uid == 2:
                raise RuntimeError("text is too long")
            self.sent.append(uid)

        users = {1: due_settings(), 2: due_settings(), 3: due_settings()}
        with self.assertLogs("funding-bot.notifier", level="ERROR"):
            stats = await self.run_notify(users, send=send)

        self.assertEqual(sorted(self.sent), [1, 3])
        self.assertEqual(stats["failed"], [2])
        self.assertEqual(sorted(stats["sent"]), [1, 3])

    async def test_blocked_user_is_reported(self):
        async def send(uid, text):
            raise Blocked()

        turned_off = []
        stats = await self.run_notify(
            {1: due_settings()},
            send=send,
            is_blocked=lambda e: isinstance(e, Blocked),
            on_blocked=lambda uid, s: turned_off.append(uid),
        )

        self.assertEqual(stats["blocked"], [1])
        self.assertEqual(turned_off, [1])

    async def test_not_sent_twice_in_same_hour(self):
        users = {1: due_settings()}
        sent_hours = {}
        await self.run_notify(users, sent_hours=sent_hours)
        await self.run_notify(users, sent_hours=sent_hours)
        self.assertEqual(self.sent, [1])

    async def test_notifications_off_is_skipped(self):
        stats = await self.run_notify({1: due_settings(notify_enabled=False)})
        self.assertEqual(stats["sent"], [])

    async def test_user_not_due_is_skipped(self):
        # цель :10, сейчас :40 — окно давно закрылось
        stats = await self.run_notify({1: due_settings(digest_before_hour_minutes=50)})
        self.assertEqual(stats["sent"], [])

    async def test_late_tick_still_sends(self):
        """Цикл задержался: цель :35, сейчас :40 — дайджест всё равно уходит."""
        stats = await self.run_notify({1: due_settings(digest_before_hour_minutes=25)})
        self.assertEqual(stats["sent"], [1])

    async def test_other_timezone_user_is_not_due(self):
        # UTC+4: локальное время 16:40 -> цель совпадает, но час другой
        s = due_settings(utc_offset_hours=4)
        sent_hours = {}
        await self.run_notify({1: s}, sent_hours=sent_hours)
        self.assertEqual(self.sent, [1])

    async def test_empty_selection_sends_nothing_but_marks_hour(self):
        sent_hours = {}
        stats = await self.run_notify(
            {1: due_settings(vol_threshold_usdt=10_000_000_000)},
            sent_hours=sent_hours,
        )
        self.assertEqual(stats["empty"], [1])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(sent_hours), 1)

    async def test_broken_thresholds_user_no_longer_floods(self):
        """Пользователь 7202439497: пороги +1/+1 ловили весь рынок."""
        captured = []

        async def send(uid, text):
            captured.append(text)

        s = due_settings(pos_threshold=1.0, neg_threshold=1.0, vol_threshold_usdt=0)
        await self.run_notify({1: s}, send=send)
        self.assertEqual(captured, [])  # ставки -0.9 не проходят порог ±1.0


if __name__ == "__main__":
    unittest.main()
