import unittest
from datetime import datetime, timedelta, timezone

from bk.timeparse import normalize_queue_start, parse_duration_seconds, parse_friendly_start, parse_memory_mb


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

    def test_friendly_now_uses_the_active_five_minute_interval(self):
        now = datetime(2030, 1, 1, 12, 41, 23, tzinfo=timezone.utc)

        self.assertEqual(parse_friendly_start("now", now), datetime(2030, 1, 1, 12, 40, tzinfo=timezone.utc))

    def test_friendly_relative_time_rounds_forward(self):
        now = datetime(2030, 1, 1, 12, 41, 23, tzinfo=timezone.utc)

        self.assertEqual(parse_friendly_start("+30m", now), datetime(2030, 1, 1, 13, 15, tzinfo=timezone.utc))

    def test_friendly_relative_time_does_not_drop_fractional_seconds(self):
        now = datetime(2030, 1, 1, 12, 40, 0, 1, tzinfo=timezone.utc)

        self.assertEqual(parse_friendly_start("+30m", now), datetime(2030, 1, 1, 13, 15, tzinfo=timezone.utc))

    def test_queue_start_floors_now_but_ceils_a_future_value(self):
        now = datetime(2030, 1, 1, 12, 41, 23, tzinfo=timezone.utc)

        self.assertEqual(normalize_queue_start(now, now), datetime(2030, 1, 1, 12, 40, tzinfo=timezone.utc))
        self.assertEqual(
            normalize_queue_start(now + timedelta(minutes=1), now),
            datetime(2030, 1, 1, 12, 45, tzinfo=timezone.utc),
        )

    def test_queue_start_ceils_fractional_future_boundary(self):
        now = datetime(2030, 1, 1, 12, 40, tzinfo=timezone.utc)
        future = datetime(2030, 1, 1, 12, 45, 0, 1, tzinfo=timezone.utc)

        self.assertEqual(
            normalize_queue_start(future, now),
            datetime(2030, 1, 1, 12, 50, tzinfo=timezone.utc),
        )

    def test_friendly_clock_and_calendar_forms_are_aligned(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)

        for value in ("20:00", "tomorrow 09:00", "07-13 20:00", "+1h"):
            with self.subTest(value=value):
                parsed = parse_friendly_start(value, now)
                self.assertEqual(int(parsed.timestamp()) % 300, 0)
                self.assertGreaterEqual(parsed, now - timedelta(minutes=5))

    def test_friendly_clock_accepts_short_day_and_clock_forms(self):
        now = datetime(2030, 1, 1, 12, 40, tzinfo=timezone.utc)
        local = now.astimezone()

        expected_tomorrow = local.replace(
            day=local.day + 1, hour=9, minute=0
        ).astimezone(timezone.utc)
        for value in ("t 9", "tmr 9am", "tom 09:00", "tomorrow 9"):
            with self.subTest(value=value):
                self.assertEqual(parse_friendly_start(value, now), expected_tomorrow)
        self.assertEqual(
            parse_friendly_start("9pm", now),
            local.replace(hour=21, minute=0).astimezone(timezone.utc),
        )
        self.assertEqual(
            parse_friendly_start("21", now),
            local.replace(hour=21, minute=0).astimezone(timezone.utc),
        )
        self.assertEqual(
            parse_friendly_start("07-13 9:30pm", now),
            local.replace(month=7, day=13, hour=21, minute=30).astimezone(
                timezone.utc
            ),
        )

    def test_friendly_time_error_lists_supported_examples(self):
        with self.assertRaisesRegex(ValueError, "tomorrow 09:00"):
            parse_friendly_start("hwo", datetime(2030, 1, 1, tzinfo=timezone.utc))

        with self.assertRaisesRegex(ValueError, "5-minute boundary"):
            parse_friendly_start("2030-01-01T12:41:00Z", datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_read_only_time_parser_can_select_aligned_past_intervals(self):
        now = datetime(2030, 1, 1, 12, 40, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ValueError, "before the current"):
            parse_friendly_start("2029-12-31T23:55:00Z", now)
        self.assertEqual(
            parse_friendly_start(
                "2029-12-31T23:55:00Z",
                now,
                allow_past=True,
            ),
            datetime(2029, 12, 31, 23, 55, tzinfo=timezone.utc),
        )

    def test_custom_granularity_controls_friendly_and_queued_times(self):
        now = datetime(2030, 1, 1, 12, 47, 23, tzinfo=timezone.utc)

        self.assertEqual(
            parse_friendly_start("now", now, slot_minutes=10),
            datetime(2030, 1, 1, 12, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_friendly_start("+30m", now, slot_minutes=10),
            datetime(2030, 1, 1, 13, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(
            normalize_queue_start(now + timedelta(minutes=1), now, slot_minutes=10),
            datetime(2030, 1, 1, 12, 50, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "10-minute boundary"):
            parse_friendly_start("2030-01-01T12:45:00Z", now, slot_minutes=10)


if __name__ == "__main__":
    unittest.main()
