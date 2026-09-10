"""Смоук по связке bot.py: импорт, клавиатуры, санитайз при сохранении.

Токен подставляем фейковый — сеть при импорте не трогается.
"""

import os
import sys
import tempfile
import unittest

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

    def test_offset_txt_formats_sign(self):
        self.assertEqual(botmod.offset_txt(UserSettings(utc_offset_hours=3)), "UTC +3")
        self.assertEqual(botmod.offset_txt(UserSettings(utc_offset_hours=-5)), "UTC -5")


class TestTickerQuery(unittest.IsolatedAsyncioTestCase):
    async def test_non_ticker_text_is_ignored(self):
        handled = await botmod.handle_ticker_query(None, "какой-то текст с пробелами")
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
