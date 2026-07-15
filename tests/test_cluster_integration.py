import json
import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cluster import (
    ClusterConfig,
    ClusterNode,
    _invoke_idempotent_write,
    run_cluster_cli,
)
from bk.granularity import ceil_to_slot
from bk.models import BookingError
from bk.timeparse import parse_iso, to_iso, utc_now


NODE_RUNNER = """\
import os
import sys

import bk.cli
import bk.node_identity
import bk.service


def identity():
    return {
        "schema": 1,
        "id": os.environ["GPUBK_TEST_NODE_ID"],
        "hostname": os.environ["GPUBK_TEST_NODE_NAME"],
    }


bk.node_identity.stable_node_identity = identity
bk.service.stable_node_identity = identity
bk.cli.stable_node_identity = identity
raise SystemExit(bk.cli.main(sys.argv[1:]))
"""


class ClusterProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runner = self.root / "node_runner.py"
        self.runner.write_text(NODE_RUNNER, encoding="utf-8")
        self.first = ClusterNode(
            "gpu-a",
            "a" * 20,
            "ssh",
            "gpu-a",
            "/usr/local/bin/bk",
            0,
            5,
        )
        self.second = ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "gpu-b",
            "/usr/local/bin/bk",
            1,
            5,
        )
        self.config = ClusterConfig(
            self.root / "cluster.json",
            (self.first, self.second),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def node_command(self, node, argv):
        environment = dict(os.environ)
        environment.update(
            {
                "BK_CLUSTER_DISABLE": "1",
                "BK_DATA_DIR": str(self.root / node.name),
                "BK_GPU_COUNT": "1",
                "BK_MAX_SHARED_USERS": "2",
                "GPUBK_TEST_NODE_ID": node.node_id,
                "GPUBK_TEST_NODE_NAME": node.name,
            }
        )
        return [sys.executable, str(self.runner), *argv], environment

    def test_automatic_booking_uses_two_independent_node_ledgers(self):
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=self.config),
            mock.patch(
                "bk.cluster_transport.node_command",
                side_effect=self.node_command,
            ),
        ):
            first_output = StringIO()
            with redirect_stdout(first_output):
                self.assertEqual(
                    run_cluster_cli(["book", "1", "30m", "--mode", "x", "--json"]),
                    0,
                )
            second_output = StringIO()
            with redirect_stdout(second_output):
                self.assertEqual(
                    run_cluster_cli(["book", "1", "30m", "--mode", "x", "--json"]),
                    0,
                )

        first = json.loads(first_output.getvalue())
        second = json.loads(second_output.getvalue())
        self.assertEqual(first["node"]["name"], "gpu-a")
        self.assertEqual(second["node"]["name"], "gpu-b")
        for node in (self.first, self.second):
            ledger = json.loads(
                (self.root / node.name / "ledger.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(ledger["reservations"]), 1)

    def test_readiness_check_skips_disabled_node_without_starting_a_process(self):
        disabled = ClusterNode(
            self.second.name,
            self.second.node_id,
            self.second.transport,
            self.second.target,
            self.second.executable,
            self.second.priority,
            self.second.timeout_seconds,
            False,
        )
        config = ClusterConfig(self.config.path, (self.first, disabled))
        contacted = []

        def command(node, argv):
            contacted.append(node.name)
            return self.node_command(node, argv)

        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster_transport.node_command", side_effect=command),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["check", "--json"]), 0)

        result = json.loads(output.getvalue())
        self.assertTrue(result["ready"])
        self.assertEqual(contacted, ["gpu-a"])
        self.assertEqual(result["summary"]["enabled"], 1)
        self.assertEqual(result["summary"]["disabled"], 1)
        self.assertEqual(result["nodes"][1]["status"], "disabled")

    def test_concurrent_exclusive_writes_serialize_without_overlap(self):
        def submit(_index):
            operation_id = str(uuid.uuid4())
            return _invoke_idempotent_write(
                self.first,
                ["x", "1", "30m", "--op-id", operation_id, "--json"],
                operation_id,
            )

        with mock.patch(
            "bk.cluster_transport.node_command",
            side_effect=self.node_command,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                replies = list(executor.map(submit, range(2)))

        self.assertTrue(all(reply.error is None for reply in replies))
        reservations = sorted(
            (reply.payload["reservation"] for reply in replies),
            key=lambda item: item["start_at"],
        )
        self.assertLessEqual(
            parse_iso(reservations[0]["end_at"]),
            parse_iso(reservations[1]["start_at"]),
        )
        ledger = json.loads(
            (self.root / self.first.name / "ledger.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(ledger["reservations"]), 2)

    def test_explicit_operation_replay_is_pinned_to_its_original_node(self):
        arguments = [
            "book",
            "1",
            "30m",
            "--mode",
            "x",
            "--op-id",
            "stable-cluster-operation",
            "--json",
        ]
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=self.config),
            mock.patch(
                "bk.cluster_transport.node_command",
                side_effect=self.node_command,
            ),
        ):
            outputs = []
            for _attempt in range(2):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run_cluster_cli(arguments), 0)
                outputs.append(json.loads(output.getvalue()))

        self.assertEqual([item["node"]["name"] for item in outputs], ["gpu-a", "gpu-a"])
        first_ledger = json.loads(
            (self.root / self.first.name / "ledger.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(first_ledger["reservations"]), 1)
        second_path = self.root / self.second.name / "ledger.json"
        if second_path.exists():
            second_ledger = json.loads(second_path.read_text(encoding="utf-8"))
            self.assertEqual(second_ledger["reservations"], [])

    def test_explicit_operation_replay_rejects_a_different_request(self):
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=self.config),
            mock.patch(
                "bk.cluster_transport.node_command",
                side_effect=self.node_command,
            ),
        ):
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    run_cluster_cli(
                        [
                            "book",
                            "1",
                            "30m",
                            "--mode",
                            "x",
                            "--op-id",
                            "stable-cluster-operation",
                            "--json",
                        ]
                    ),
                    0,
                )
            with self.assertRaisesRegex(BookingError, "different write"):
                run_cluster_cli(
                    [
                        "book",
                        "1",
                        "35m",
                        "--mode",
                        "x",
                        "--op-id",
                        "stable-cluster-operation",
                        "--json",
                    ]
                )

        first_ledger = json.loads(
            (self.root / self.first.name / "ledger.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(first_ledger["reservations"]), 1)

    def test_node_qualified_edit_and_cancel_are_retry_safe_end_to_end(self):
        future_start = to_iso(ceil_to_slot(utc_now() + timedelta(minutes=15)))
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=self.config),
            mock.patch(
                "bk.cluster_transport.node_command",
                side_effect=self.node_command,
            ),
        ):
            created_output = StringIO()
            with redirect_stdout(created_output):
                self.assertEqual(
                    run_cluster_cli(
                        [
                            "book",
                            "1",
                            "30m",
                            "--mode",
                            "x",
                            "--start",
                            future_start,
                            "--op-id",
                            "lifecycle-create",
                            "-j",
                        ]
                    ),
                    0,
                )
            created = json.loads(created_output.getvalue())
            node_name = created["node"]["name"]
            short_id = created["result"]["reservation"]["short_id"]
            qualified = f"{node_name}/{short_id}"

            edit_results = []
            for _attempt in range(2):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        run_cluster_cli(
                            [
                                "edit",
                                qualified,
                                "--duration",
                                "35m",
                                "--op-id",
                                "lifecycle-edit",
                                "-j",
                            ]
                        ),
                        0,
                    )
                edit_results.append(json.loads(output.getvalue()))

            cancel_results = []
            for _attempt in range(2):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        run_cluster_cli(
                            [
                                "cancel",
                                qualified,
                                "--op-id",
                                "lifecycle-cancel",
                                "-j",
                            ]
                        ),
                        0,
                    )
                cancel_results.append(json.loads(output.getvalue()))

            with self.assertRaisesRegex(BookingError, "different write"):
                run_cluster_cli(
                    [
                        "cancel",
                        f"{node_name}/deadbeef",
                        "--op-id",
                        "lifecycle-cancel",
                        "-j",
                    ]
                )

        self.assertEqual(
            [item["node"] for item in edit_results + cancel_results],
            [node_name] * 4,
        )
        self.assertEqual(
            [item["operation_id"] for item in edit_results],
            ["lifecycle-edit"] * 2,
        )
        self.assertEqual(
            [item["operation_id"] for item in cancel_results],
            ["lifecycle-cancel"] * 2,
        )
        self.assertEqual(
            edit_results[0]["result"]["reservation"]["id"],
            edit_results[1]["result"]["reservation"]["id"],
        )
        self.assertEqual(
            cancel_results[0]["result"]["reservation"]["id"],
            cancel_results[1]["result"]["reservation"]["id"],
        )
        self.assertEqual(cancel_results[1]["result"]["kind"], "cancellation_result")


if __name__ == "__main__":
    unittest.main()
