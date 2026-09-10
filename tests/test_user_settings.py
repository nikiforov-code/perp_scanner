import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import user_settings as us
from user_settings import UserSettings, sanitize, DEFAULT_UTC_OFFSET


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
