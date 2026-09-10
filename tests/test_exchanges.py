import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchanges.types import to_percent, to_int_ms, okx_instid_to_symbol
from exchanges.okx import select_instruments


class TestHelpers(unittest.TestCase):
    def test_to_percent_converts_share_to_percent(self):
        self.assertAlmostEqual(to_percent("0.0001"), 0.01)
        self.assertAlmostEqual(to_percent(-0.005), -0.5)

    def test_to_int_ms_handles_junk(self):
        self.assertEqual(to_int_ms(None), 0)
        self.assertEqual(to_int_ms(""), 0)
        self.assertEqual(to_int_ms("  "), 0)
        self.assertEqual(to_int_ms("1700000000000"), 1700000000000)
        self.assertEqual(to_int_ms("1.7e12"), 1700000000000)

    def test_okx_instid_to_symbol(self):
        self.assertEqual(okx_instid_to_symbol("BTC-USDT-SWAP"), "BTCUSDT")
        self.assertEqual(okx_instid_to_symbol("BTC-USD-SWAP"), "")


class TestSelectInstruments(unittest.TestCase):
    """OKX опрашивается поштучно, поэтому отбор монет — ключевая функция."""

    def setUp(self):
        self.inst_ids = ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"]
        self.vol_map = {"AAAUSDT": 9_000_000.0, "BBBUSDT": 2_000_000.0, "CCCUSDT": 0.0}

    def test_keeps_only_liquid_instruments(self):
        got = select_instruments(self.inst_ids, self.vol_map, 5_000_000)
        self.assertEqual(got, ["AAA-USDT-SWAP"])

    def test_lower_threshold_keeps_more(self):
        got = select_instruments(self.inst_ids, self.vol_map, 1_000_000)
        self.assertEqual(sorted(got), ["AAA-USDT-SWAP", "BBB-USDT-SWAP"])

    def test_no_count_cap(self):
        """Старый код резал список до 350 и терял 113 монет OKX."""
        inst_ids = [f"C{i}-USDT-SWAP" for i in range(500)]
        vol_map = {f"C{i}USDT": 10_000_000.0 for i in range(500)}
        self.assertEqual(len(select_instruments(inst_ids, vol_map, 1_000_000)), 500)

    def test_unknown_volume_is_dropped(self):
        got = select_instruments(["ZZZ-USDT-SWAP"], {}, 1_000_000)
        self.assertEqual(got, [])

    def test_zero_threshold_keeps_everything_known(self):
        got = select_instruments(self.inst_ids, self.vol_map, 0)
        self.assertEqual(len(got), 3)


if __name__ == "__main__":
    unittest.main()
