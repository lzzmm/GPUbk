import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from bk.models import MODE_SHARED
from bk.presets import (
    delete_preset,
    get_preset,
    learned_profile,
    load_preset_document,
    preset_suggestion,
    save_preset,
)
from bk.timeparse import to_iso, utc_now


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "presets.json"

    def tearDown(self):
        self.tmp.cleanup()

    def profile(self, name="train"):
        return {
            "name": name,
            "mode": MODE_SHARED,
            "count": 2,
            "duration_seconds": 3600,
            "expected_memory_mb": 12 * 1024,
            "share_units": 2,
            "preferred_gpus": None,
            "excluded_gpus": [3],
        }

    def test_save_load_replace_and_delete_are_private(self):
        saved = save_preset(self.profile(), self.path)
        replacement = save_preset(
            {**self.profile(), "duration_seconds": 7200}, self.path
        )

        self.assertEqual(saved["created_at"], replacement["created_at"])
        self.assertEqual(get_preset("train", self.path)["duration_seconds"], 7200)
        self.assertEqual(len(load_preset_document(self.path)["presets"]), 1)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertTrue(delete_preset("train", self.path))
        self.assertFalse(delete_preset("train", self.path))

    def test_three_matching_reservations_produce_a_learned_default(self):
        now = utc_now()
        reservations = []
        for index in range(3):
            start = now + timedelta(hours=index)
            reservations.append(
                {
                    "id": f"reservation-{index}",
                    "uid": 1001,
                    "username": "user1001",
                    "mode": MODE_SHARED,
                    "gpus": [0, 1],
                    "share_units": 2,
                    "expected_memory_mb": 12 * 1024,
                    "start_at": to_iso(start),
                    "end_at": to_iso(start + timedelta(hours=1)),
                    "created_at": to_iso(now + timedelta(minutes=index)),
                }
            )
        ledger = {"reservations": reservations}

        learned = learned_profile(ledger, 1001)
        suggestion = preset_suggestion(ledger, 1001, [])

        self.assertEqual(learned["observations"], 3)
        self.assertEqual(learned["count"], 2)
        self.assertEqual(learned["duration_seconds"], 3600)
        self.assertIsNone(learned["preferred_gpus"])
        self.assertEqual(learned["excluded_gpus"], [])
        self.assertIsNotNone(suggestion)

    def test_concurrent_saves_preserve_every_distinct_preset(self):
        names = [f"train-{index}" for index in range(16)]

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(
                executor.map(
                    lambda name: save_preset(self.profile(name), self.path),
                    names,
                )
            )

        document = load_preset_document(self.path)
        self.assertEqual([item["name"] for item in document["presets"]], sorted(names))

    def test_saved_placement_specific_preset_suppresses_same_learned_pattern(self):
        now = utc_now()
        reservation = {
            "id": "reservation",
            "uid": 1001,
            "username": "user1001",
            "mode": MODE_SHARED,
            "gpus": [0, 1],
            "share_units": 2,
            "expected_memory_mb": 12 * 1024,
            "start_at": to_iso(now),
            "end_at": to_iso(now + timedelta(hours=1)),
            "created_at": to_iso(now),
        }
        ledger = {"reservations": [reservation, reservation, reservation]}
        preset = {
            **self.profile(),
            "preferred_gpus": [0, 1],
            "excluded_gpus": [],
        }

        self.assertIsNone(preset_suggestion(ledger, 1001, [preset]))


if __name__ == "__main__":
    unittest.main()
