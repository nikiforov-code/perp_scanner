import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import user_settings as us
from user_settings import (
    UserSettings,
    sanitize,
    DEFAULT_UTC_OFFSET,
    DEFAULT_RADAR_RATE,
    DEFAULT_RADAR_VOL,
)


class TestSanitize(unittest.TestCase):
    def test_positive_negative_threshold_is_flipped(self):
        """Из-за порога -1.00, введённого как +1.00, каждый час падала вся рассылка."""
        s = sanitize(UserSettings(pos_threshold=1.0, neg_threshold=1.0))
        self.assertEqual(s.pos_threshold, 1.0)
        self.assertEqual(s.neg_threshold, -1.0)

    def test_normal_thresholds_untouched(self):
        s = sanitize(UserSettings(pos_threshold=0.45, neg_threshold=-0.25))
        self.assertEqual((s.pos_threshold, s.neg_threshold), (0.45, -0.25))

    def test_negative_positive_threshold_is_flipped(self):
        s = sanitize(UserSettings(pos_threshold=-0.5, neg_threshold=-0.5))
        self.assertEqual(s.pos_threshold, 0.5)

    def test_missing_timezone_gets_default(self):
        s = sanitize(UserSettings(utc_offset_hours=None))
        self.assertEqual(s.utc_offset_hours, DEFAULT_UTC_OFFSET)

    def test_timezone_is_clamped(self):
        self.assertEqual(sanitize(UserSettings(utc_offset_hours=99)).utc_offset_hours, 12)
        self.assertEqual(sanitize(UserSettings(utc_offset_hours=-99)).utc_offset_hours, -12)

    def test_digest_minute_is_clamped(self):
        self.assertEqual(sanitize(UserSettings(digest_before_hour_minutes=0)).digest_before_hour_minutes, 1)
        self.assertEqual(sanitize(UserSettings(digest_before_hour_minutes=90)).digest_before_hour_minutes, 59)

    def test_negative_volume_becomes_zero(self):
        self.assertEqual(sanitize(UserSettings(vol_threshold_usdt=-5)).vol_threshold_usdt, 0.0)


class TestRadarSettings(unittest.TestCase):
    def test_positive_radar_rate_is_flipped(self):
        """Радар работает только по отрицательной стороне."""
        self.assertEqual(sanitize(UserSettings(radar_rate=1.5)).radar_rate, -1.5)

    def test_negative_radar_rate_kept(self):
        self.assertEqual(sanitize(UserSettings(radar_rate=-2.4)).radar_rate, -2.4)

    def test_zero_radar_rate_falls_back_to_default(self):
        """Ноль после миграции означал бы «любая отрицательная ставка» — это спам."""
        self.assertEqual(sanitize(UserSettings(radar_rate=0)).radar_rate, DEFAULT_RADAR_RATE)

    def test_zero_radar_volume_falls_back_to_default(self):
        self.assertEqual(sanitize(UserSettings(radar_vol=0)).radar_vol, DEFAULT_RADAR_VOL)

    def test_defaults_match_owner_choice(self):
        s = sanitize(UserSettings())
        self.assertEqual(s.radar_rate, -1.5)
        self.assertEqual(s.radar_vol, 5_000_000)


class TestRadarStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        us.DB_PATH = self.tmp.name
        us.db_init()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_mark_and_load(self):
        us.db_radar_mark(1, "STORJUSDT", 1000)
        us.db_radar_mark(2, "STORJUSDT", 2000)
        marks = us.db_radar_load_all()
        self.assertEqual(marks[(1, "STORJUSDT")], 1000)
        self.assertEqual(marks[(2, "STORJUSDT")], 2000)

    def test_mark_overwrites(self):
        us.db_radar_mark(1, "AUSDT", 1000)
        us.db_radar_mark(1, "AUSDT", 5000)
        self.assertEqual(us.db_radar_load_all()[(1, "AUSDT")], 5000)

    def test_purge_removes_old_only(self):
        us.db_radar_mark(1, "OLDUSDT", 1000)
        us.db_radar_mark(1, "NEWUSDT", 9000)
        us.db_radar_purge(before_ts=5000)
        marks = us.db_radar_load_all()
        self.assertNotIn((1, "OLDUSDT"), marks)
        self.assertIn((1, "NEWUSDT"), marks)

    def test_empty_store(self):
        self.assertEqual(us.db_radar_load_all(), {})

    def test_settings_roundtrip_includes_radar(self):
        us.db_save_user(5, UserSettings(radar_rate=-3.0, radar_vol=20_000_000))
        got = us.db_load_user(5)
        self.assertEqual(got.radar_rate, -3.0)
        self.assertEqual(got.radar_vol, 20_000_000)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        us.DB_PATH = self.tmp.name
        us.db_init()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_roundtrip(self):
        s = UserSettings(pos_threshold=0.7, neg_threshold=-0.3, utc_offset_hours=5)
        us.db_save_user(111, s)
        got = us.db_load_user(111)
        self.assertEqual(got.pos_threshold, 0.7)
        self.assertEqual(got.neg_threshold, -0.3)
        self.assertEqual(got.utc_offset_hours, 5)

    def test_missing_user_is_none(self):
        self.assertIsNone(us.db_load_user(999))

    def test_load_all_returns_every_user(self):
        """После рестарта бот должен знать всех, а не только написавших ему."""
        us.db_save_user(1, UserSettings())
        us.db_save_user(2, UserSettings())
        us.db_save_user(3, UserSettings())
        everyone = us.db_load_all()
        self.assertEqual(sorted(everyone.keys()), [1, 2, 3])

    def test_load_all_sanitizes(self):
        us.db_save_user(7, UserSettings(pos_threshold=1.0, neg_threshold=1.0, utc_offset_hours=None))
        loaded = us.db_load_all()[7]
        self.assertEqual(loaded.neg_threshold, -1.0)
        self.assertEqual(loaded.utc_offset_hours, DEFAULT_UTC_OFFSET)

    def test_load_user_sanitizes(self):
        us.db_save_user(8, UserSettings(neg_threshold=2.0))
        self.assertEqual(us.db_load_user(8).neg_threshold, -2.0)


if __name__ == "__main__":
    unittest.main()
