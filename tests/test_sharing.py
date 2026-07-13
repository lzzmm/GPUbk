import unittest

from bk.sharing import (
    inferred_share_memory_mb,
    parse_share_units,
    reservation_share_units,
)


class SharingTests(unittest.TestCase):
    def test_share_slots_accept_only_whole_integer_units(self):
        self.assertEqual(parse_share_units(3, 4), 3)
        self.assertEqual(parse_share_units("3", 4), 3)
        for value in ("3/4", "75%", 1.5, True, "auto"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "whole number"
            ):
                parse_share_units(value, 4)

    def test_legacy_reservation_defaults_to_one_unit_and_invalid_data_fails_closed(self):
        self.assertEqual(reservation_share_units({}, 4), 1)
        self.assertEqual(reservation_share_units({"share_units": "bad"}, 4), 4)
        self.assertEqual(reservation_share_units({"share_units": 1.5}, 4), 4)

    def test_inferred_memory_scales_with_reserved_share(self):
        self.assertEqual(inferred_share_memory_mb(24000, 4, 1), 6000)
        self.assertEqual(inferred_share_memory_mb(24000, 4, 3), 18000)


if __name__ == "__main__":
    unittest.main()
