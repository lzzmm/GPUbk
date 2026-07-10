import unittest

from bk.timeparse import parse_duration_seconds, parse_memory_mb


class TimeParsingTests(unittest.TestCase):
    def test_compound_duration(self):
        self.assertEqual(parse_duration_seconds("1h30m"), 90 * 60)
        self.assertEqual(parse_duration_seconds("1d2h5m"), (24 * 60 + 2 * 60 + 5) * 60)

    def test_invalid_compound_duration(self):
        for value in ("1h30h", "1hour", "30", "", "0m"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duration_seconds(value)

    def test_memory_units(self):
        self.assertEqual(parse_memory_mb("12g"), 12 * 1024)
        self.assertEqual(parse_memory_mb("1.5GiB"), 1536)
        self.assertEqual(parse_memory_mb("4096m"), 4096)


if __name__ == "__main__":
    unittest.main()
