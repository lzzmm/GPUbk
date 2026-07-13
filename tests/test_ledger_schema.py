import re
import unittest

from bk.ledger_schema import validate_ledger_document


def reservation(**updates):
    record = {
        "id": "11111111-1111-4111-8111-111111111111",
        "uid": 1001,
        "username": "user1001",
        "gpus": [0],
        "mode": "shared",
        "start_at": "2030-01-01T12:00:00Z",
        "end_at": "2030-01-01T13:00:00Z",
        "status": "active",
        "created_at": "2030-01-01T11:00:00Z",
        "updated_at": "2030-01-01T11:00:00Z",
    }
    record.update(updates)
    return record


class LedgerSchemaTests(unittest.TestCase):
    def test_unknown_fields_remain_forward_compatible(self):
        ledger = {
            "version": 1,
            "future_top_level": {"kept": True},
            "reservations": [reservation(future_field={"kept": True})],
        }

        validate_ledger_document(ledger)

        self.assertTrue(ledger["future_top_level"]["kept"])
        self.assertTrue(ledger["reservations"][0]["future_field"]["kept"])

    def test_semantic_fields_fail_closed_with_precise_paths(self):
        cases = (
            ("non-object", "reservations[0] must be an object"),
            (reservation(uid=True), "reservations[0].uid"),
            (reservation(gpus=[]), "reservations[0].gpus"),
            (reservation(gpus=[0, 0]), "must not contain duplicates"),
            (reservation(mode="future-mode"), "reservations[0].mode"),
            (reservation(status="future-status"), "reservations[0].status"),
            (reservation(start_at="broken"), "reservations[0].start_at"),
            (
                reservation(end_at="2030-01-01T11:00:00Z"),
                "end_at must be later",
            ),
            (reservation(job=[]), "reservations[0].job"),
            (
                reservation(expected_memory_mb=0),
                "reservations[0].expected_memory_mb",
            ),
        )
        for record, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, re.escape(message)):
                    validate_ledger_document({"version": 1, "reservations": [record]})

    def test_duplicate_reservation_and_edit_operation_ids_are_rejected(self):
        first = reservation()
        second = reservation(username="other")
        with self.assertRaisesRegex(ValueError, "duplicates reservation"):
            validate_ledger_document({"version": 1, "reservations": [first, second]})

        history = [
            {"op_id": "edit-1", "signature": "a"},
            {"op_id": "edit-1", "signature": "b"},
        ]
        with self.assertRaisesRegex(ValueError, "duplicates operation ID"):
            validate_ledger_document(
                {
                    "version": 1,
                    "reservations": [reservation(edit_operations=history)],
                }
            )

    def test_operation_ids_are_unique_per_uid_but_not_globally(self):
        first = reservation(op_id="agent-request")
        same_uid = reservation(
            id="22222222-2222-4222-8222-222222222222",
            op_id="agent-request",
        )
        with self.assertRaisesRegex(ValueError, "for UID 1001"):
            validate_ledger_document(
                {"version": 1, "reservations": [first, same_uid]}
            )

        other_uid = reservation(
            id="33333333-3333-4333-8333-333333333333",
            uid=1002,
            op_id="agent-request",
        )
        validate_ledger_document(
            {"version": 1, "reservations": [first, other_uid]}
        )


if __name__ == "__main__":
    unittest.main()
