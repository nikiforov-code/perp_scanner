"""Смоук по связке bot.py: импорт, клавиатуры, санитайз при сохранении.

Токен подставляем фейковый — сеть при импорте не трогается.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:AAFakeTokenForImportCheck")

import user_settings as store
import bot as botmod
from user_settings import UserSettings


class TestBotWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        store.DB_PATH = self.tmp.name
        store.db_init()
        botmod.USER_SETTINGS.clear()
        botmod.SENT_HOURS.clear()

    def tearDown(self):
        botmod.USER_SETTINGS.clear()
        os.unlink(self.tmp.name)

    def test_handlers_registered(self):
        self.assertGreaterEqual(len(botmod.dp.message.handlers), 10)

    def test_new_user_gets_default_timezone(self):
        """Раньше пользователь без ответа про таймзону застревал навсегда."""
        s = botmod.get_settings(555)
        self.assertEqual(s.utc_offset_hours, store.DEFAULT_UTC_OFFSET)

    def test_save_settings_sanitizes_thresholds(self):
        s = botmod.get_settings(556)
        s.neg_threshold = 1.0
        botmod.save_settings(556, s)
        self.assertEqual(botmod.USER_SETTINGS[556].neg_threshold, -1.0)
        self.assertEqual(store.db_load_user(556).neg_threshold, -1.0)

    def test_filters_keyboard_builds(self):
        kb = botmod.filters_kb(557)
        labels = [b.text for row in kb.keyboard for b in row]
        self.assertTrue(any("Таймзона" in x for x in labels))
        self.assertTrue(any("Порог +" in x for x in labels))

    def test_disable_notifications_persists(self):
        s = botmod.get_settings(558)
        botmod._disable_notifications(558, s)
        self.assertFalse(store.db_load_user(558).notify_enabled)

    def test_min_volume_uses_softest_threshold(self):
        botmod.USER_SETTINGS[1] = UserSettings(vol_threshold_usdt=10_000_000)
        botmod.USER_SETTINGS[2] = UserSettings(vol_threshold_usdt=2_000_000)
        self.assertEqual(botmod.min_volume_for_okx(), 2_000_000)

    def test_radar_buttons_present(self):
        labels = [b.text for row in botmod.filters_kb(560).keyboard for b in row]
        self.assertTrue(any("Радар ставка" in x for x in labels))
        self.assertTrue(any("Радар объём" in x for x in labels))

    def test_radar_defaults_shown_on_keyboard(self):
        labels = [b.text for row in botmod.filters_kb(561).keyboard for b in row]
        self.assertTrue(any("-1.50%" in x for x in labels))
        self.assertTrue(any("5M" in x for x in labels))

    def test_radar_rate_sign_fixed_on_save(self):
        s = botmod.get_settings(562)
        s.radar_rate = 2.5          # пользователь ввёл без минуса
        botmod.save_settings(562, s)
        self.assertEqual(store.db_load_user(562).radar_rate, -2.5)

    def test_radar_marks_dict_exists(self):
        self.assertIsInstance(botmod.RADAR_MARKS, dict)

    def test_offset_txt_formats_sign(self):
        self.assertEqual(botmod.offset_txt(UserSettings(utc_offset_hours=3)), "UTC +3")
        self.assertEqual(botmod.offset_txt(UserSettings(utc_offset_hours=-5)), "UTC -5")


def _flood(retry_after):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage
    return TelegramRetryAfter(
        method=SendMessage(chat_id=1, text="x"), message="Too Many Requests", retry_after=retry_after
    )


class TestFloodControl(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_flood_control(self):
        send = AsyncMock(side_effect=[_flood(3), "ok"])
        sleep = AsyncMock()
        with patch.object(botmod.bot, "send_message", send), patch("bot.asyncio.sleep", sleep):
            await botmod.send_message_retrying(chat_id=1, text="x")
        self.assertEqual(send.await_count, 2)
        sleep.assert_awaited_once_with(3)

    async def test_digest_chunk_not_duplicated(self):
        """Раньше 429 на второй части заставлял слать весь дайджест заново."""
        send = AsyncMock(side_effect=["ok", _flood(1), "ok"])
        text = "\n".join(["x" * 100] * 60)  # заведомо больше одной части
        with patch.object(botmod.bot, "send_message", send), patch("bot.asyncio.sleep", AsyncMock()):
            await botmod.send_chunks(1, text)
        sent = [c.kwargs["text"] for c in send.await_args_list]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1], sent[2])  # повторена только упавшая часть

    async def test_gives_up_on_long_wait(self):
        send = AsyncMock(side_effect=[_flood(botmod.MAX_RETRY_WAIT_SEC + 1)])
        with patch.object(botmod.bot, "send_message", send), patch("bot.asyncio.sleep", AsyncMock()):
            with self.assertRaises(Exception):
                await botmod.send_message_retrying(chat_id=1, text="x")
        self.assertEqual(send.await_count, 1)

    async def test_gives_up_after_attempts(self):
        send = AsyncMock(side_effect=[_flood(1)] * botmod.SEND_ATTEMPTS)
        with patch.object(botmod.bot, "send_message", send), patch("bot.asyncio.sleep", AsyncMock()):
            with self.assertRaises(Exception):
                await botmod.send_message_retrying(chat_id=1, text="x")
        self.assertEqual(send.await_count, botmod.SEND_ATTEMPTS)


class TestTickerQuery(unittest.IsolatedAsyncioTestCase):
    async def test_non_ticker_text_is_ignored(self):
        handled = await botmod.handle_ticker_query(None, "какой-то текст с пробелами")
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
